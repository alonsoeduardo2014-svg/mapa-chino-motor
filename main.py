"""
Motor del Mapa del mundo en chino — cobro de la versión completa
=================================================================
La página del mapa vive aparte (sitio estático en Render, abre al instante).
Este motor solo hace lo que necesita protección:

  - Revisa si la persona tiene la suscripción del mapa activa.
  - Le entrega los audios de estados y provincias SOLO si está suscrita.
  - Confirma el pago con PayPal justo después de suscribirse.
  - Recibe los avisos de PayPal (cobro mensual, cancelación, falta de pago).

Las cuentas son las mismas de Supabase que usa el traductor. La suscripción del
mapa se guarda en su propia tabla ("suscripciones_mapa"), así no se cruza con
los planes del traductor.

VARIABLES DE ENTORNO (en Render → Environment):
    SUPABASE_URL, SUPABASE_SECRET_KEY,
    PAYPAL_CLIENT_ID, PAYPAL_SECRET, PAYPAL_WEBHOOK_ID, PLAN_MAPA

ARCHIVOS:
    main.py, requirements.txt y audios.zip (los audios de estados y provincias).
    Render los saca del zip al construir (ver "Build Command" en el instructivo).
"""

import os
import re
import time
import logging
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx
from supabase import create_client, Client

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

PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")
PAYPAL_SECRET = os.environ.get("PAYPAL_SECRET")
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID")
PLAN_MAPA = (os.environ.get("PLAN_MAPA") or "").strip()          # el plan de PayPal de $3 USD/mes
PAYPAL_API = os.environ.get("PAYPAL_API", "https://api-m.paypal.com")  # cobros reales (modo "Live")

TABLA = "suscripciones_mapa"
CARPETA_AUDIOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audios")
NOMBRE_PAQUETE = re.compile(r"^[A-Za-z0-9_-]{1,40}-\d{1,4}$")

# Para no preguntarle a Supabase en cada audio: se recuerda unos minutos quién es quién.
RECUERDA_SEG = 300
_tokens: dict[str, tuple[float, object]] = {}      # token -> (hasta cuándo, usuario)
_estados: dict[str, tuple[float, bool]] = {}       # id de usuario -> (hasta cuándo, suscrito)


# ---------------------------------------------------------------------------
# Cuentas y suscripción
# ---------------------------------------------------------------------------

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


def fila_de(id_usuario: str) -> dict | None:
    r = cliente_supabase.table(TABLA).select("*").eq("usuario", id_usuario).limit(1).execute()
    return r.data[0] if r.data else None


def esta_suscrito(id_usuario: str, forzar: bool = False) -> bool:
    ahora = time.time()
    guardado = _estados.get(id_usuario)
    if guardado and guardado[0] > ahora and not forzar:
        return guardado[1]
    fila = fila_de(id_usuario)
    ok = bool(fila and fila.get("estado") == "activa")
    # Revisión de seguridad: si hace más de un día que no sabemos nada de esa
    # suscripción, le preguntamos a PayPal cómo está (por si se perdió un aviso).
    if fila and fila.get("id_suscripcion") and _paypal_configurado():
        try:
            act = datetime.fromisoformat(str(fila.get("actualizado")).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - act > timedelta(days=1):
                sincronizar(fila["id_suscripcion"])
                fila = fila_de(id_usuario)
                ok = bool(fila and fila.get("estado") == "activa")
        except Exception:
            logger.exception("No se pudo revisar la suscripción con PayPal")
    _estados[id_usuario] = (ahora + RECUERDA_SEG, ok)
    return ok


def guardar(id_usuario: str, email: str | None, id_suscripcion: str, estado: str) -> None:
    datos = {"usuario": id_usuario, "id_suscripcion": id_suscripcion, "estado": estado,
             "actualizado": datetime.now(timezone.utc).isoformat()}
    if email:
        datos["email"] = email
    cliente_supabase.table(TABLA).upsert(datos, on_conflict="usuario").execute()
    _estados.pop(id_usuario, None)
    logger.info("Suscripción del mapa %s -> %s (usuario %s)", id_suscripcion, estado, id_usuario)


def cambiar_estado(id_suscripcion: str, estado: str) -> None:
    r = (cliente_supabase.table(TABLA)
         .update({"estado": estado, "actualizado": datetime.now(timezone.utc).isoformat()})
         .eq("id_suscripcion", id_suscripcion).execute())
    for f in (r.data or []):
        _estados.pop(f.get("usuario"), None)
    logger.info("Suscripción del mapa %s -> %s", id_suscripcion, estado)


# ---------------------------------------------------------------------------
# PayPal
# ---------------------------------------------------------------------------

def _paypal_configurado() -> bool:
    return bool(PAYPAL_CLIENT_ID and PAYPAL_SECRET)


def _token_paypal() -> str:
    r = httpx.post(f"{PAYPAL_API}/v1/oauth2/token", auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
                   data={"grant_type": "client_credentials"}, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


def consultar_suscripcion(id_suscripcion: str) -> dict:
    """Pregunta directo a PayPal cómo está una suscripción (nadie puede fingir que pagó)."""
    r = httpx.get(f"{PAYPAL_API}/v1/billing/subscriptions/{id_suscripcion}",
                  headers={"Authorization": f"Bearer {_token_paypal()}"}, timeout=20)
    r.raise_for_status()
    return r.json()


def sincronizar(id_suscripcion: str) -> None:
    """Deja la suscripción igual a lo que dice PayPal. Ignora las que no son del mapa."""
    sub = consultar_suscripcion(id_suscripcion)
    if sub.get("plan_id") != PLAN_MAPA:
        return
    if sub.get("status") == "ACTIVE" and sub.get("custom_id"):
        guardar(sub["custom_id"], None, sub["id"], "activa")
    elif sub.get("status") in ("CANCELLED", "SUSPENDED", "EXPIRED"):
        cambiar_estado(sub["id"], "inactiva")
    # cualquier otro estado (por ejemplo, recién aprobada): no se cambia nada


def firma_valida(headers, evento: dict) -> bool:
    """Le pregunta a PayPal si el aviso de verdad viene de él."""
    if not PAYPAL_WEBHOOK_ID:
        return False
    cuerpo = {
        "auth_algo": headers.get("paypal-auth-algo"),
        "cert_url": headers.get("paypal-cert-url"),
        "transmission_id": headers.get("paypal-transmission-id"),
        "transmission_sig": headers.get("paypal-transmission-sig"),
        "transmission_time": headers.get("paypal-transmission-time"),
        "webhook_id": PAYPAL_WEBHOOK_ID,
        "webhook_event": evento,
    }
    r = httpx.post(f"{PAYPAL_API}/v1/notifications/verify-webhook-signature",
                   headers={"Authorization": f"Bearer {_token_paypal()}"}, json=cuerpo, timeout=20)
    return r.status_code == 200 and r.json().get("verification_status") == "SUCCESS"


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
    return {"estado": "ok", "supabase": cliente_supabase is not None, "paypal": _paypal_configurado(),
            "aviso_paypal": bool(PAYPAL_WEBHOOK_ID), "plan": bool(PLAN_MAPA), "paquetes_de_audio": n}


@app.get("/estado")
def estado(authorization: str | None = Header(default=None)):
    """La página pregunta aquí si la persona tiene la versión completa."""
    usuario = usuario_de(authorization)
    if usuario is None:
        return {"sesion": False, "suscrito": False}
    try:
        ok = esta_suscrito(usuario.id, forzar=True)
    except Exception:
        logger.exception("No se pudo leer la suscripción")
        raise HTTPException(status_code=500, detail="No se pudo revisar tu suscripción. Intenta de nuevo.")
    return {"sesion": True, "suscrito": ok, "email": usuario.email}


@app.get("/audio/{paquete}")
def audio(paquete: str, authorization: str | None = Header(default=None)):
    """Entrega un paquete de audios de estados/provincias, solo a suscriptores."""
    if not NOMBRE_PAQUETE.match(paquete) or paquete.startswith("mundo-"):
        raise HTTPException(status_code=404, detail="No existe.")
    usuario = usuario_de(authorization)
    if usuario is None:
        raise HTTPException(status_code=401, detail="Inicia sesión.")
    if not esta_suscrito(usuario.id):
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
    if cliente_supabase is None or not _paypal_configurado() or not PLAN_MAPA:
        raise HTTPException(status_code=500, detail="El motor no tiene configurado PayPal o Supabase.")
    usuario = usuario_de(authorization)
    if usuario is None:
        raise HTTPException(status_code=401, detail="Inicia sesión para activar tu suscripción.")
    try:
        sub = consultar_suscripcion(datos.id_suscripcion)
    except Exception:
        logger.exception("No se pudo consultar la suscripción en PayPal")
        raise HTTPException(status_code=502, detail="No pudimos confirmar tu pago con PayPal. Si ya pagaste, se activará solo en unos minutos.")
    if sub.get("custom_id") != usuario.id:
        raise HTTPException(status_code=403, detail="Esta suscripción no pertenece a tu cuenta.")
    if sub.get("plan_id") != PLAN_MAPA:
        raise HTTPException(status_code=400, detail="Esta suscripción no es la del mapa.")
    if sub.get("status") not in ("ACTIVE", "APPROVED"):
        raise HTTPException(status_code=400, detail="Tu pago todavía no aparece como activo. Espera unos minutos.")
    guardar(usuario.id, usuario.email, sub["id"], "activa")
    return {"ok": True, "suscrito": True}


@app.post("/paypal/webhook")
async def webhook(request: Request):
    """PayPal avisa aquí cuando alguien se suscribe, paga su mes, cancela o deja de pagar."""
    evento = await request.json()
    try:
        valido = firma_valida(request.headers, evento)
    except Exception:
        logger.exception("Error verificando aviso de PayPal")
        raise HTTPException(status_code=500, detail="No se pudo verificar el aviso.")
    if not valido:
        logger.warning("Aviso de PayPal con firma inválida, ignorado")
        raise HTTPException(status_code=400, detail="Firma inválida.")

    tipo = evento.get("event_type", "")
    rec = evento.get("resource", {}) or {}
    logger.info("Aviso de PayPal: %s", tipo)
    try:
        if tipo.startswith("BILLING.SUBSCRIPTION."):
            if rec.get("plan_id") and rec.get("plan_id") != PLAN_MAPA:
                return {"ok": True, "ignorado": "no es del mapa"}
            if tipo == "BILLING.SUBSCRIPTION.ACTIVATED" and rec.get("custom_id"):
                guardar(rec["custom_id"], None, rec["id"], "activa")
            elif tipo in ("BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.SUSPENDED",
                          "BILLING.SUBSCRIPTION.EXPIRED"):
                cambiar_estado(rec.get("id"), "inactiva")
        elif tipo == "PAYMENT.SALE.COMPLETED" and rec.get("billing_agreement_id"):
            sincronizar(rec["billing_agreement_id"])   # cobro mensual: seguir activo
    except Exception:
        logger.exception("Error aplicando aviso de PayPal")
        raise HTTPException(status_code=500, detail="Error aplicando el aviso.")
    return {"ok": True}
