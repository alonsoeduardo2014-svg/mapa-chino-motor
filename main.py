"""
Motor del Mapa del mundo en chino — versión completa con el Acceso Total
========================================================================
La página del mapa vive aparte (sitio estático en Render, abre al instante).
Este motor solo hace lo que necesita protección:

  - Revisa si la persona tiene el Acceso Total (una sola suscripción para
    todas las apps del Instituto Alonso Laoshi).
  - Le entrega los audios de estados y provincias SOLO si lo tiene.
  - Confirma el pago con PayPal justo después de suscribirse.
  - Recibe los avisos de PayPal (cobro mensual, cancelación, falta de pago).
  - Cancela la suscripción cuando la persona toca "Cancelar mi suscripción".

La suscripción la maneja acceso_total.py (el mismo archivo en las tres apps).

VARIABLES DE ENTORNO (en Render → Environment):
    SUPABASE_URL, SUPABASE_SECRET_KEY,
    PAYPAL_CLIENT_ID, PAYPAL_SECRET, PAYPAL_WEBHOOK_ID,
    PLAN_TOTAL, PLAN_PLUS

ARCHIVOS:
    main.py, acceso_total.py, requirements.txt y audios.zip.
"""

import os
import re
import time
import logging

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from supabase import create_client, Client

import acceso_total as acceso

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("motor-mapa")

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")
cliente_supabase: Client | None = (
    create_client(SUPABASE_URL, SUPABASE_SECRET_KEY) if SUPABASE_URL and SUPABASE_SECRET_KEY else None
)
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID")

CARPETA_AUDIOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audios")
NOMBRE_PAQUETE = re.compile(r"^[A-Za-z0-9_-]{1,40}-\d{1,4}$")

# Para no preguntarle a Supabase en cada audio: se recuerda unos minutos quién es quién.
RECUERDA_SEG = 300
_tokens: dict[str, tuple[float, object]] = {}      # token -> (hasta cuándo, usuario)


def usuario_de(authorization: str | None):
    """Lee el token de sesión que manda la página y regresa el usuario (o None)."""
    if cliente_supabase is None or not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    if not token:
        return None
    ahora = time.time()
    guardado = _tokens.get(token)
    if guardado and guardado[0] > ahora:
        return guardado[1]
    try:
        r = cliente_supabase.auth.get_user(token)
        usuario = r.user if r else None
    except Exception:
        usuario = None
    if usuario is not None:
        if len(_tokens) > 5000:
            _tokens.clear()
        _tokens[token] = (ahora + RECUERDA_SEG, usuario)
    return usuario


# ---------------------------------------------------------------------------
# Páginas del motor
# ---------------------------------------------------------------------------

app = FastAPI(title="Motor del Mapa del mundo en chino")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                   allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"])


@app.get("/")
@app.get("/salud")
def salud():
    """Para revisar que el motor está despierto y bien configurado."""
    n = len([f for f in os.listdir(CARPETA_AUDIOS) if f.endswith(".mp3")]) if os.path.isdir(CARPETA_AUDIOS) else 0
    return {"estado": "ok", "supabase": cliente_supabase is not None, "paypal": acceso.paypal_configurado(),
            "aviso_paypal": bool(PAYPAL_WEBHOOK_ID), "plan_total": bool(acceso.PLAN_TOTAL),
            "plan_plus": bool(acceso.PLAN_PLUS), "paquetes_de_audio": n}


@app.get("/planes")
def planes():
    """Los planes del Acceso Total (para dibujar los botones de PayPal)."""
    return acceso.planes_publicos()


@app.get("/estado")
def estado(authorization: str | None = Header(default=None)):
    """La página pregunta aquí si la persona tiene la versión completa."""
    usuario = usuario_de(authorization)
    if usuario is None:
        return {"sesion": False, "suscrito": False}
    try:
        acceso._olvidar(usuario.id)
        e = acceso.estado_de(usuario.id)
    except Exception:
        logger.exception("No se pudo leer la suscripción")
        raise HTTPException(status_code=500, detail="No se pudo revisar tu suscripción. Intenta de nuevo.")
    # estado: "activa" (se cobra cada mes) o "cancelada" (ya no se cobra; tiene todo hasta "hasta")
    return {"sesion": True, "suscrito": e["suscrito"], "email": usuario.email,
            "estado": e["estado"], "hasta": e["hasta"], "plan": e["plan"]}


@app.get("/audio/{paquete}")
def audio(paquete: str, authorization: str | None = Header(default=None)):
    """Entrega un paquete de audios de estados/provincias, solo a suscriptores."""
    if not NOMBRE_PAQUETE.match(paquete) or paquete.startswith("mundo-"):
        raise HTTPException(status_code=404, detail="No existe.")
    usuario = usuario_de(authorization)
    if usuario is None:
        raise HTTPException(status_code=401, detail="Inicia sesión.")
    try:
        suscrito = acceso.estado_de(usuario.id)["suscrito"]
    except Exception:
        logger.exception("No se pudo leer la suscripción")
        raise HTTPException(status_code=500, detail="No se pudo revisar tu suscripción.")
    if not suscrito:
        raise HTTPException(status_code=403, detail="Necesitas la versión completa.")
    ruta = os.path.join(CARPETA_AUDIOS, paquete + ".mp3")
    if not os.path.isfile(ruta):
        raise HTTPException(status_code=404, detail="No existe.")
    return FileResponse(ruta, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})


class Confirmacion(BaseModel):
    id_suscripcion: str


@app.post("/paypal/confirmar")
def confirmar(datos: Confirmacion, authorization: str | None = Header(default=None)):
    """La página llama aquí justo después de suscribirse, para activar al instante."""
    usuario = usuario_de(authorization)
    if usuario is None:
        raise HTTPException(status_code=401, detail="Inicia sesión para activar tu suscripción.")
    try:
        r = acceso.confirmar(usuario.id, usuario.email, datos.id_suscripcion)
    except acceso.ErrorAcceso as e:
        raise HTTPException(status_code=e.status, detail=e.mensaje)
    return {"ok": True, "suscrito": True, "plan": r["plan"], "hasta": r["hasta"]}


@app.post("/cancelar")
def cancelar(authorization: str | None = Header(default=None)):
    """Botón "Cancelar mi suscripción": se cancela en PayPal (ya no se cobra) y
    la persona conserva todas las apps hasta el día que ya pagó."""
    usuario = usuario_de(authorization)
    if usuario is None:
        raise HTTPException(status_code=401, detail="Inicia sesión para cancelar tu suscripción.")
    try:
        return acceso.cancelar(usuario.id)
    except acceso.ErrorAcceso as e:
        raise HTTPException(status_code=e.status, detail=e.mensaje)


@app.post("/paypal/webhook")
async def webhook(request: Request):
    """PayPal avisa aquí cuando alguien se suscribe, paga su mes, cancela o deja de pagar."""
    evento = await request.json()
    try:
        valido = acceso.verificar_firma(request.headers, evento, PAYPAL_WEBHOOK_ID)
    except Exception:
        logger.exception("Error verificando aviso de PayPal")
        raise HTTPException(status_code=500, detail="No se pudo verificar el aviso.")
    if not valido:
        logger.warning("Aviso de PayPal con firma inválida, ignorado")
        raise HTTPException(status_code=400, detail="Firma inválida.")
    logger.info("Aviso de PayPal: %s", evento.get("event_type", ""))
    try:
        acceso.aplicar_aviso(evento)
    except Exception:
        logger.exception("Error aplicando aviso de PayPal")
        raise HTTPException(status_code=500, detail="Error aplicando el aviso.")
    return {"ok": True}
