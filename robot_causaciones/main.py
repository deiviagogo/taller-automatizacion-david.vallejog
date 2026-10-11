"""Robot de causación de facturas en SICOF.

Uso (desde esta carpeta):  uv run python main.py
Entrada : datos/por_radicar.xlsx   (libro de OneDrive descargado con ese nombre)
Salida  : salida/bot.log, salida/resultados.xlsx, salida/evidencias/*.png
"""
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_fixed

from libros import escribir_resultados, leer_cola
from reglas import Fila, limpiar_nit, normalizar_fecha, normalizar_valor, validar
from sicof import ERRORES_NAVEGADOR, FallaSistema, RechazoFactura, Sicof

BASE = Path(__file__).resolve().parent
ENTRADA = BASE / "datos" / "por_radicar.xlsx"
SALIDA = BASE / "salida"
EVIDENCIAS = SALIDA / "evidencias"
MAX_INTENTOS = 3

log = logging.getLogger("robot")


def configurar_log():
    SALIDA.mkdir(exist_ok=True)
    EVIDENCIAS.mkdir(exist_ok=True)
    formato = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
    # Se reescribe en cada ejecución: bot.log corresponde siempre a la última corrida.
    for h in (logging.FileHandler(SALIDA / "bot.log", mode="w", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)):
        h.setFormatter(formato)
        log.addHandler(h)
    log.setLevel(logging.INFO)


@dataclass
class Estado:
    confirmo: bool = False   # ¿ya se pulsó "Confirmar causación" en esta ejecución?


def un_intento(sicof: Sicof, fila: Fila, nit: int, estado: Estado):
    """Un intento: SIEMPRE consulta primero para no causar dos veces la misma factura."""
    existente = sicof.consultar(nit, fila.factura)
    if existente:
        # Si ya confirmamos en esta ejecución y la respuesta se perdió, la causamos nosotros.
        return ("CAUSADA" if estado.confirmo else "YA_CAUSADA"), existente

    def marcar():
        estado.confirmo = True

    numero = sicof.registrar(
        nit=nit,
        factura=fila.factura,
        valor=normalizar_valor(fila.valor_crudo),
        fecha=normalizar_fecha(fila.fecha_cruda),
        centro=fila.centro,
        antes_de_confirmar=marcar,
    )
    return "CAUSADA", numero


def capturar_pantalla(page, fila: Fila):
    try:
        nombre = re.sub(r"[^\w.-]", "_", f"{fila.id_lista}_{fila.factura}")
        ruta = EVIDENCIAS / f"{nombre}.png"
        page.screenshot(path=str(ruta), full_page=True)
        log.info("Captura de pantalla guardada en %s", ruta.relative_to(BASE))
    except Exception as exc:  # la captura nunca debe detener el lote
        log.warning("No se pudo guardar la captura de %s: %s", fila.factura, exc)


def procesar(sicof: Sicof, fila: Fila, hoy: date) -> dict:
    nit = limpiar_nit(fila.nit_crudo)
    base = {"IdLista": fila.id_lista, "Factura": fila.factura, "NIT": nit,
            "Causacion": "", "Detalle": ""}
    etiqueta = f"IdLista={fila.id_lista} factura={fila.factura} NIT={nit if nit else fila.nit_crudo}"

    motivo = validar(fila, hoy)
    if motivo:
        log.info("%s | RECHAZADA (sin tocar SICOF) | %s", etiqueta, motivo)
        return {**base, "Estado": "RECHAZADA", "Detalle": motivo}

    estado = Estado()

    def antes_de_reintentar(rs):
        log.warning("%s | reintento %d/%d tras falla de sistema | %s",
                    etiqueta, rs.attempt_number + 1, MAX_INTENTOS, rs.outcome.exception())

    try:
        for intento in Retrying(
            stop=stop_after_attempt(MAX_INTENTOS),
            wait=wait_fixed(1),
            retry=retry_if_exception_type(FallaSistema),
            before_sleep=antes_de_reintentar,
            reraise=True,
        ):
            with intento:
                try:
                    res_estado, numero = un_intento(sicof, fila, nit, estado)
                except ERRORES_NAVEGADOR as exc:
                    raise FallaSistema(f"Error de navegador/conexión: {str(exc).splitlines()[0][:200]}") from exc
        log.info("%s | %s | %s", etiqueta, res_estado, numero)
        return {**base, "Estado": res_estado, "Causacion": numero}
    except RechazoFactura as exc:
        log.info("%s | RECHAZADA | %s", etiqueta, exc)
        return {**base, "Estado": "RECHAZADA", "Detalle": str(exc)}
    except FallaSistema as exc:
        log.error("%s | FALLA_SISTEMA tras %d intentos | %s", etiqueta, MAX_INTENTOS, exc)
        capturar_pantalla(sicof.page, fila)
        return {**base, "Estado": "FALLA_SISTEMA", "Detalle": str(exc)}


def lanzar_navegador(pw, headless: bool):
    """Abre Chromium; en una máquina sin estado previo lo instala la primera vez."""
    try:
        return pw.chromium.launch(headless=headless)
    except PWError as exc:
        if "playwright install" not in str(exc) and "Executable doesn't exist" not in str(exc):
            raise
        log.warning("Chromium no está instalado; instalándolo (solo la primera vez)")
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        return pw.chromium.launch(headless=headless)


def main() -> int:
    configurar_log()
    load_dotenv(BASE / ".env")
    url = os.getenv("PORTAL_URL", "http://127.0.0.1:8020")
    usuario = os.getenv("PORTAL_USUARIO", "operador.rpa")
    clave = os.getenv("PORTAL_CLAVE", "CxpSicof2026")
    headless = os.getenv("BOT_HEADLESS", "true").strip().lower() != "false"

    hoy = date.today()
    log.info("===== Inicio de la ejecución (%s) =====", hoy.isoformat())
    cola = leer_cola(ENTRADA)
    log.info("Filas a procesar: %d", len(cola))
    resultados: list[dict] = []

    try:
        with sync_playwright() as pw:
            navegador = lanzar_navegador(pw, headless)
            page = navegador.new_context().new_page()
            page.set_default_timeout(10000)
            sicof = Sicof(page, url, usuario, clave)
            for fila in cola:
                resultados.append(procesar(sicof, fila, hoy))
                escribir_resultados(SALIDA / "resultados.xlsx", resultados)
            navegador.close()
    finally:
        escribir_resultados(SALIDA / "resultados.xlsx", resultados)
        resumen: dict[str, int] = {}
        for r in resultados:
            resumen[r["Estado"]] = resumen.get(r["Estado"], 0) + 1
        log.info("Resumen: %s", resumen)
        log.info("===== Fin de la ejecución =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
