"""Automatización de pantalla de SICOF.

Todo se ubica por lo que conserva la interfaz v2: etiquetas visibles, texto de
los botones, títulos y el rol `alert` de los mensajes. Nunca por id, clase o
estructura HTML.
"""
import logging
import re
import time
import unicodedata
from datetime import date
from urllib.parse import quote

from playwright.sync_api import Error as PWError
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PWTimeout

log = logging.getLogger("robot")

RE_CAUSACION = re.compile(r"CXP-\d{4}-\d{6}")
RE_NO_DISPONIBLE = re.compile(r"no disponible|SICOF-503", re.I)

ESPERA_PANTALLA = 15      # segundos para que cargue una pantalla / el maestro de proveedores
ESPERA_RESULTADO = 12     # segundos para ver la respuesta tras un clic


class FallaSistema(Exception):
    """Pantalla de servicio no disponible, tiempo agotado o error de conexión."""


class RechazoFactura(Exception):
    """SICOF o el robot rechazan la factura por sus datos: no se reintenta."""


def _sin_tildes(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


class Sicof:
    def __init__(self, page: Page, url: str, usuario: str, clave: str):
        self.page = page
        self.url = url.rstrip("/")
        self.usuario = usuario
        self.clave = clave

    # ------------------------------------------------------------ utilidades
    def _texto(self) -> str:
        return self.page.locator("body").inner_text()

    def _revisar_servicio(self):
        if RE_NO_DISPONIBLE.search(self._texto()):
            raise FallaSistema("SICOF respondió: Servicio temporalmente no disponible (SICOF-503)")

    def _alertas(self) -> list[str]:
        """Textos no vacíos de los elementos con rol alert."""
        textos = [t.strip() for t in self.page.get_by_role("alert").all_inner_texts()]
        return [" ".join(t.split()) for t in textos if t]

    def _en_login(self) -> bool:
        return self.page.locator("input[type=password]").count() > 0

    def _ir(self, ruta: str):
        """Va a una ruta; si SICOF manda al ingreso, inicia sesión y reintenta."""
        for _ in range(2):
            respuesta = self.page.goto(f"{self.url}{ruta}")
            if respuesta is not None and respuesta.status >= 500:
                raise FallaSistema(f"SICOF respondió HTTP {respuesta.status}")
            if self._en_login():
                self.login()
                continue
            self._revisar_servicio()
            return
        raise FallaSistema("SICOF no permitió abrir la pantalla tras iniciar sesión")

    # ---------------------------------------------------------------- sesión
    def login(self):
        log.info("Iniciando sesión en SICOF")
        self.page.goto(f"{self.url}/login")
        self._revisar_servicio()
        self.page.get_by_label(re.compile(r"usuario", re.I)).first.fill(self.usuario)
        self.page.get_by_label(re.compile(r"contrase[ñn]a", re.I)).first.fill(self.clave)
        self.page.get_by_role("button", name=re.compile(r"ingresar", re.I)).first.click()
        limite = time.monotonic() + ESPERA_PANTALLA
        while time.monotonic() < limite:
            if not self._en_login():
                return
            if self._alertas():
                raise FallaSistema(f"No fue posible ingresar a SICOF: {self._alertas()[0]}")
            time.sleep(0.2)
        raise FallaSistema("No fue posible ingresar a SICOF (tiempo agotado)")

    # -------------------------------------------------- consulta (idempotencia)
    def consultar(self, nit: int, factura: str):
        """Número de causación si ya existe una con el mismo NIT y número de factura."""
        self._ir(f"/causaciones?factura={quote(factura)}")
        self.page.wait_for_load_state("load")
        encontrada = self._buscar_en_tabla(nit, factura)
        if encontrada:
            return encontrada
        return self._buscar_en_texto(nit, factura)

    def _buscar_en_tabla(self, nit: int, factura: str):
        encabezados = [_sin_tildes(h).strip() for h in self.page.get_by_role("columnheader").all_inner_texts()]
        if not {"causacion", "nit", "factura"} <= set(encabezados):
            return None
        i_c, i_n, i_f = (encabezados.index(c) for c in ("causacion", "nit", "factura"))
        for fila in self.page.get_by_role("row").all():
            celdas = [c.strip() for c in fila.get_by_role("cell").all_inner_texts()]
            if len(celdas) != len(encabezados):
                continue
            nit_celda = re.sub(r"\D", "", celdas[i_n])
            if nit_celda == str(nit) and celdas[i_f] == factura:
                m = RE_CAUSACION.search(celdas[i_c])
                if m:
                    return m.group(0)
        return None

    def _buscar_en_texto(self, nit: int, factura: str):
        """Respaldo independiente de la estructura: parte el texto en bloques por causación."""
        texto = self._texto()
        marcas = list(RE_CAUSACION.finditer(texto))
        patron_factura = re.compile(rf"(?<![\w-]){re.escape(factura)}(?![\w-])")
        for k, m in enumerate(marcas):
            fin = marcas[k + 1].start() if k + 1 < len(marcas) else len(texto)
            bloque = texto[m.start():fin]
            numeros = {re.sub(r"[.,]", "", t) for t in re.findall(r"\d[\d.,]*\d|\d", bloque)}
            if str(nit) in numeros and patron_factura.search(bloque):
                return m.group(0)
        return None

    # ---------------------------------------------------------------- registro
    def _campo(self, patron: str):
        return self.page.get_by_label(re.compile(patron, re.I)).first

    def registrar(self, nit: int, factura: str, valor: int, fecha: date, centro: str,
                  antes_de_confirmar=None) -> str:
        """Recorre formulario -> confirmación -> causación registrada. Devuelve CXP-AAAA-NNNNNN."""
        self._ir("/causaciones/nueva")

        # Los campos están deshabilitados mientras se carga el maestro de proveedores.
        campo_nit = self._campo(r"nit del proveedor|^\s*nit")
        try:
            campo_nit.wait_for(state="visible", timeout=ESPERA_PANTALLA * 1000)
            expect(campo_nit).to_be_enabled(timeout=ESPERA_PANTALLA * 1000)
        except (PWTimeout, AssertionError) as exc:
            self._revisar_servicio()
            raise FallaSistema("El formulario de causación no se habilitó a tiempo") from exc

        selector = self._campo(r"centro de costo")
        codigo_valor = self._valor_centro(selector, centro)

        campo_nit.fill(str(nit))
        self._campo(r"n[uú]mero de factura").fill(factura)
        campo_fecha = self._campo(r"fecha de factura")
        if (campo_fecha.get_attribute("type") or "").lower() == "date":
            campo_fecha.fill(fecha.isoformat())
        else:
            campo_fecha.fill(fecha.strftime("%d/%m/%Y"))
        self._campo(r"valor total").fill(str(valor))
        selector.select_option(value=codigo_valor)

        self.page.get_by_role("button", name=re.compile(r"^\s*continuar", re.I)).first.click()
        confirmar = self.page.get_by_role("button", name=re.compile(r"confirmar causaci[oó]n", re.I))
        self._esperar_confirmacion(confirmar)

        if antes_de_confirmar:
            antes_de_confirmar()
        confirmar.first.click()
        return self._esperar_causacion()

    def _valor_centro(self, selector, codigo: str) -> str:
        """Valor de la opción cuyo código es `codigo`; si no existe, es rechazo de la factura."""
        opciones = selector.locator("option").evaluate_all(
            "els => els.map(e => [e.value, (e.textContent || '').trim()])"
        )
        patron = re.compile(rf"^{re.escape(codigo)}(?![\w])", re.I)
        for valor, etiqueta in opciones:
            if not valor:
                continue
            if valor.strip().upper() == codigo.upper() or patron.match(etiqueta):
                return valor
        raise RechazoFactura(f"El centro de costo {codigo} no existe en la lista de SICOF")

    def _esperar_confirmacion(self, confirmar):
        """Tras Continuar: o aparece la confirmación o SICOF devuelve un mensaje de rechazo."""
        limite = time.monotonic() + ESPERA_RESULTADO
        while time.monotonic() < limite:
            self._revisar_servicio()
            if confirmar.count() > 0 and confirmar.first.is_visible():
                return
            alertas = self._alertas()
            if alertas:
                raise RechazoFactura(alertas[0])
            time.sleep(0.2)
        raise FallaSistema("SICOF no respondió al pulsar Continuar (tiempo agotado)")

    def _esperar_causacion(self) -> str:
        """Tras Confirmar causación: número de causación, rechazo o falla de sistema."""
        limite = time.monotonic() + ESPERA_RESULTADO
        while time.monotonic() < limite:
            self._revisar_servicio()
            m = RE_CAUSACION.search(self._texto())
            if m:
                return m.group(0)
            alertas = self._alertas()
            if alertas:
                raise RechazoFactura(alertas[0])
            time.sleep(0.2)
        raise FallaSistema("SICOF no mostró el número de causación (tiempo agotado)")


ERRORES_NAVEGADOR = (PWTimeout, PWError)
