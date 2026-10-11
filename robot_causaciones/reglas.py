"""Normalización y validaciones que se hacen ANTES de tocar SICOF."""
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from openpyxl.utils.datetime import from_excel

_RE_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Fila:
    """Una fila de PorRadicar, con los valores tal como llegaron del libro."""
    id_lista: int
    factura: str
    nit_crudo: Any
    proveedor: str
    valor_crudo: Any
    centro: str
    fecha_cruda: Any


def texto(valor: Any) -> str:
    """Texto limpio; un número entero guardado como float pierde el '.0'."""
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor).strip()


def limpiar_nit(valor: Any) -> Optional[int]:
    """NIT como entero sin separadores: 900123456, '900,123,456' o '900.123.456'."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor if valor > 0 else None
    if isinstance(valor, float):
        return int(valor) if valor.is_integer() and valor > 0 else None
    s = str(valor).strip()
    if re.fullmatch(r"\d+\.0+", s):          # '900123456.0'
        s = s.split(".")[0]
    s = re.sub(r"[.,\s]", "", s)
    if s.isdigit() and int(s) > 0:
        return int(s)
    return None


def normalizar_valor(valor: Any) -> Optional[int]:
    """Entero o None si no es un número entero (el '> 0' se valida aparte)."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor
    if isinstance(valor, float):
        return int(valor) if valor.is_integer() else None
    try:
        d = Decimal(str(valor).strip())
    except InvalidOperation:
        return None
    if not d.is_finite() or d != d.to_integral_value():
        return None
    return int(d)


def normalizar_fecha(valor: Any) -> Optional[date]:
    """Fecha real o None. Acepta fechas de Excel y texto yyyy-MM-dd."""
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        try:                                   # fecha guardada como número de serie
            return from_excel(valor).date()
        except (ValueError, OverflowError, TypeError):
            return None
    s = texto(valor)
    if not _RE_FECHA.match(s):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def validar(fila: Fila, hoy: date) -> str:
    """Motivo del rechazo, o '' si la factura puede ir a SICOF."""
    if not fila.factura:
        return "El número de factura es obligatorio"
    if limpiar_nit(fila.nit_crudo) is None:
        return f"NIT inválido: {texto(fila.nit_crudo)!r}"
    valor = normalizar_valor(fila.valor_crudo)
    if valor is None or valor <= 0:
        return f"El valor debe ser un número entero mayor que cero: {texto(fila.valor_crudo)!r}"
    fecha = normalizar_fecha(fila.fecha_cruda)
    if fecha is None:
        return f"La fecha de factura no es una fecha yyyy-MM-dd válida: {texto(fila.fecha_cruda)!r}"
    if fecha > hoy:
        return f"La fecha de factura ({fecha.isoformat()}) es posterior a la fecha de ejecución ({hoy.isoformat()})"
    if not fila.centro:
        return "El centro de costo es obligatorio"
    return ""
