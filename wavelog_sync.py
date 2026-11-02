#!/usr/bin/env python3
"""
Sincroniza QSOs nuevos de Wavelog hacia QRZ.com y LogDeArgentina (LdA).

Flujo:
  1. Descarga el log de Wavelog por API (api/get_contacts_adif), una llamada por station_id.
  2. Valida los campos obligatorios de cada QSO.
  3. Compara contra el registro local (estado/registro.json) y arma un único ADIF
     con los QSOs válidos que todavía no se subieron a algún destino.
  4. Sube a LdA por API (un QSO por llamada) y a QRZ por Playwright (cuenta gratuita, sin API).
  5. Guarda en el registro el resultado por QSO y por destino.

Uso:
  python wavelog_sync.py --estaciones   # lista los station_id de tu Wavelog
  python wavelog_sync.py --dry-run      # descarga, valida y genera el ADIF, sin subir ni registrar
  python wavelog_sync.py                # corrida completa, todas las estaciones
  python wavelog_sync.py --sin-qrz      # no sube a QRZ
  python wavelog_sync.py --sin-lda      # no sube a LdA
  python wavelog_sync.py --qrz-login    # login manual en QRZ (sólo la primera vez o si vence)
  python wavelog_sync.py --qrz-corregir # corrige en QRZ grids/counties (también corre tras cada subida)

Estaciones (por station_id, nombre del perfil o indicativo; sin distinguir mayúsculas):
  python wavelog_sync.py --todas                          # todas (es el default)
  python wavelog_sync.py --estacion Casa                  # sólo esa
  python wavelog_sync.py --estacion 1,11 --estacion LU0XXX/M
  python wavelog_sync.py --excluir "Contest"              # todas menos esa
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
REGISTRO_PATH = BASE_DIR / "estado" / "registro.json"
EXPORTS_DIR = BASE_DIR / "exports"
QRZ_PERFIL_DIR = BASE_DIR / "estado" / "qrz_navegador"

DESTINOS = ("qrz", "lda")

# Estados por destino:
#   pendiente -> todavía no se intentó o falló por un error transitorio (se reintenta)
#   ok        -> subido (incluye "duplicado" informado por el destino)
#   rechazado -> el destino lo rechazó por datos (no se reintenta automáticamente)
PENDIENTE, OK, RECHAZADO = "pendiente", "ok", "rechazado"


# --------------------------------------------------------------------------- config

def cargar_config():
    load_dotenv(BASE_DIR / ".env")

    def req(nombre):
        valor = os.getenv(nombre, "").strip()
        if not valor:
            sys.exit(f"Falta {nombre} en .env (ver .env.example)")
        return valor

    desde = req("SYNC_DESDE")
    try:
        desde_dt = datetime.strptime(desde, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        sys.exit("SYNC_DESDE debe tener formato 'AAAA-MM-DD HH:MM' (UTC)")

    return {
        "wavelog_url": req("WAVELOG_URL").rstrip("/"),
        "wavelog_key": req("WAVELOG_API_KEY"),
        "desde": desde_dt,
        "lda_user": os.getenv("LDA_USER", "").strip(),
        "lda_pass": os.getenv("LDA_PASS", "").strip(),
        "lda_micall": os.getenv("LDA_MICALL", "").strip().upper(),
        "lda_qsl_msg": os.getenv("LDA_QSL_MSG", "").strip(),
        # Nombre exacto del libro en QRZ (como aparece en Awards e Import). Default: "<LDA_MICALL> Log".
        "qrz_libro": os.getenv("QRZ_LIBRO", "").strip() or f"{os.getenv('LDA_MICALL', '').strip().upper()} Log",
    }


# --------------------------------------------------------------------------- ADIF

TAG_RE = re.compile(r"<([A-Za-z0-9_]+)(?::(\d+))?(?::[A-Za-z])?>")
FIN_VALOR_RE = re.compile(r"\s*(?:<|$)")


def _leer_valor(texto, pos, largo):
    """Devuelve (valor, nueva_pos). ADIF declara el largo, pero según quién lo genera
    puede ser en caracteres o en bytes UTF-8. Se prueban ambos y se elige el que deja
    el puntero justo antes del próximo tag."""
    por_chars = texto[pos:pos + largo]
    candidatos = [(por_chars, pos + largo)]
    if len(por_chars.encode("utf-8")) != largo:
        n_bytes, fin = 0, pos
        while fin < len(texto) and n_bytes < largo:
            n_bytes += len(texto[fin].encode("utf-8"))
            fin += 1
        candidatos.insert(0, (texto[pos:fin], fin))
    for valor, fin in candidatos:
        # match en la posición, sin copiar el resto del texto (con logs grandes era cuadrático)
        if FIN_VALOR_RE.match(texto, fin):
            return valor, fin
    return candidatos[-1]


def parsear_adif(texto):
    """Devuelve lista de dicts {campos: {...}, raw: '<...><eor>'}."""
    m = re.search(r"<eoh>", texto, re.IGNORECASE)
    pos = m.end() if m else 0
    registros, campos, inicio = [], {}, None
    while True:
        tag = TAG_RE.search(texto, pos)
        if not tag:
            break
        nombre = tag.group(1).upper()
        if inicio is None:
            inicio = tag.start()
        if nombre == "EOR":
            registros.append({"campos": campos, "raw": texto[inicio:tag.end()].strip()})
            campos, inicio, pos = {}, None, tag.end()
            continue
        largo = int(tag.group(2) or 0)
        valor, pos = _leer_valor(texto, tag.end(), largo)
        campos[nombre] = valor.strip()
    return registros


def escribir_adif(registros, ruta, comentario):
    ahora = datetime.now(timezone.utc).strftime("%Y%m%d %H%M%S")
    header = (
        f"{comentario}\n"
        "<ADIF_VER:5>3.1.4\n"
        "<PROGRAMID:12>wavelog_sync\n"
        f"<CREATED_TIMESTAMP:15>{ahora}\n"
        "<EOH>\n\n"
    )
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(header + "\n\n".join(r["raw"] for r in registros) + "\n", encoding="utf-8")


def clave_qso(c):
    """Identificador estable de un QSO (Wavelog no expone su ID interno en el ADIF)."""
    return "|".join([
        c.get("CALL", "").upper(),
        c.get("QSO_DATE", ""),
        c.get("TIME_ON", "")[:4],
        c.get("BAND", "").lower(),
        c.get("MODE", "").upper(),
    ])


def fecha_qso(c):
    try:
        return datetime.strptime(c["QSO_DATE"] + c["TIME_ON"][:4], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None


# --------------------------------------------------------------------------- validación

def es_argentina(c):
    return c.get("COUNTRY", "").strip().upper() == "ARGENTINA" or c.get("DXCC", "").strip() == "100"


def faltantes(c):
    obligatorios = {
        "QSO_DATE": "fecha",
        "TIME_ON": "hora",
        "CALL": "señal distintiva",
        "CQZ": "cq zone",
        "ITUZ": "itu zone",
    }
    falta = [desc for campo, desc in obligatorios.items() if not c.get(campo)]
    if es_argentina(c) and not c.get("STATE"):
        falta.append("state (Argentina)")
    if c.get("QSO_DATE") and c.get("TIME_ON") and fecha_qso(c) is None:
        falta.append("fecha/hora con formato inválido")
    return falta


# --------------------------------------------------------------------------- Wavelog

def wavelog_estaciones(cfg):
    r = requests.get(f"{cfg['wavelog_url']}/api/station_info/{cfg['wavelog_key']}", timeout=30)
    r.raise_for_status()
    return r.json()


def _coincide(estacion, criterio):
    criterio = criterio.strip().lower()
    return criterio in (
        str(estacion.get("station_id", "")).lower(),
        str(estacion.get("station_profile_name", "")).strip().lower(),
        str(estacion.get("station_callsign", "")).strip().lower(),
    )


def _criterios(valores):
    """['1,11', 'LU0XXX/M'] -> ['1', '11', 'LU0XXX/M']"""
    return [c.strip() for v in valores or [] for c in v.split(",") if c.strip()]


def elegir_estaciones(estaciones, incluir, excluir):
    """Sin --estacion se toman todas; después se quitan las de --excluir.
    Un criterio que no coincide con ninguna estación corta la ejecución, para no
    sincronizar algo distinto de lo pedido por un error de tipeo."""
    def buscar(criterio):
        encontradas = [e for e in estaciones if _coincide(e, criterio)]
        if not encontradas:
            sys.exit(f"No hay ninguna estación que coincida con '{criterio}'. Ver --estaciones.")
        return encontradas

    elegidas = estaciones
    if incluir:
        ids = {e["station_id"] for c in incluir for e in buscar(c)}
        elegidas = [e for e in estaciones if e["station_id"] in ids]
    if excluir:
        fuera = {e["station_id"] for c in excluir for e in buscar(c)}
        elegidas = [e for e in elegidas if e["station_id"] not in fuera]
    if not elegidas:
        sys.exit("La selección de estaciones quedó vacía.")
    return elegidas


def wavelog_descargar(cfg, estaciones):
    registros = []
    for e in estaciones:
        sid = e["station_id"]
        r = requests.post(
            f"{cfg['wavelog_url']}/api/get_contacts_adif",
            json={"key": cfg["wavelog_key"], "station_id": sid, "fetchfromid": 0},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        adif = data.get("adif") or ""
        if data.get("status") != "successful":
            sys.exit(f"Wavelog station_id={sid}: {data.get('message')}")
        regs = parsear_adif(adif)
        print(f"Wavelog station_id={sid} ({e.get('station_profile_name')}): "
              f"{len(regs)} QSOs descargados ({data.get('message')})")
        registros.extend(regs)
    return registros


# --------------------------------------------------------------------------- registro

def cargar_registro():
    if REGISTRO_PATH.exists():
        return json.loads(REGISTRO_PATH.read_text(encoding="utf-8"))
    return {"ultima_ejecucion": None, "qsos": {}}


def guardar_registro(reg):
    REGISTRO_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRO_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(REGISTRO_PATH)


def marcar(entrada, destino, estado, mensaje=""):
    entrada[destino] = {
        "estado": estado,
        "mensaje": mensaje,
        "fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- LdA

LDA_URL = "https://www.lda.ar/php/subeqso2.php"
LDA_OK = ("Guardado OK", "QSO duplicado", "QSO Confirmado")


def lda_modo(c):
    # ADIF guarda FT4 como MODE=MFSK/SUBMODE=FT4, PSK31 como MODE=PSK/SUBMODE=PSK31, etc.
    modo = c.get("MODE", "").upper()
    if modo in ("MFSK", "PSK") and c.get("SUBMODE"):
        return c["SUBMODE"].upper()
    return modo


def lda_subir_qso(cfg, c):
    """Devuelve (estado, mensaje)."""
    micall = c.get("STATION_CALLSIGN", "").upper() or cfg["lda_micall"]
    params = {
        "user": cfg["lda_user"],
        "pass": cfg["lda_pass"],
        "micall": micall,
        "sucall": c.get("CALL", "").upper(),
        "banda": c.get("BAND", "").lower(),
        "modo": lda_modo(c),
        "fecha": c.get("QSO_DATE", ""),
        "hora": c.get("TIME_ON", "")[:4],
        "rst": c.get("RST_SENT", ""),
    }
    if c.get("PROP_MODE", "").upper() == "SAT":  # LdA sólo admite SAT como propagación
        params["prop"] = "SAT"
    if cfg["lda_qsl_msg"]:
        params["x_qslMSG"] = cfg["lda_qsl_msg"]
    try:
        r = requests.get(LDA_URL, params=params, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        # Sin el detalle de la URL: lleva la contraseña en la query string.
        return PENDIENTE, f"error de red: {type(e).__name__}"
    texto = re.sub(r"<[^>]+>", " ", r.text)
    texto = " ".join(texto.split())
    if any(ok in texto for ok in LDA_OK):
        return OK, texto
    return RECHAZADO, texto or "respuesta vacía"


def lda_subir(cfg, pendientes, registro):
    if not (cfg["lda_user"] and cfg["lda_pass"]):
        print("LdA: faltan LDA_USER/LDA_PASS en .env, se omite.")
        return
    print(f"LdA: subiendo {len(pendientes)} QSOs...")
    for q in pendientes:
        clave = clave_qso(q["campos"])
        estado, msg = lda_subir_qso(cfg, q["campos"])
        marcar(registro["qsos"][clave], "lda", estado, msg)
        guardar_registro(registro)
        print(f"  {clave}: {estado} - {msg}")
        time.sleep(0.5)


# --------------------------------------------------------------------------- QRZ

QRZ_LOGBOOK = "https://logbook.qrz.com/"
QRZ_LOGIN = "https://www.qrz.com/login"
QRZ_OK = "uploaded successfully"


QRZ_LOGIN_ESPERA = 600  # segundos para que el usuario complete el login manual


def qrz_abrir(p, visible):
    # Perfil persistente: guarda cookies, localStorage y el "trust device" del 2FA entre corridas.
    ctx = p.chromium.launch_persistent_context(str(QRZ_PERFIL_DIR), headless=not visible)
    return ctx, (ctx.pages[0] if ctx.pages else ctx.new_page())


def qrz_logueado(page):
    """Va al logbook; hay sesión si aparece el menú Import/Export (sin sesión redirige)."""
    page.goto(QRZ_LOGBOOK, wait_until="domcontentloaded")
    try:
        page.wait_for_selector("#dropdownMenuButton", state="attached", timeout=15000)
        return True
    except Exception:  # noqa: BLE001 - timeout o redirección al login
        return False


def qrz_login_manual(p):
    """Abre una ventana en el login de QRZ y espera a que el usuario entre a mano
    (con 2FA si lo tiene). La sesión queda en el perfil; dura lo que QRZ decida."""
    ctx, page = qrz_abrir(p, visible=True)
    try:
        page.goto(QRZ_LOGIN, wait_until="domcontentloaded")
        print(f"QRZ: logueate en la ventana que se abrió (espero hasta {QRZ_LOGIN_ESPERA // 60} min).")
        limite = time.time() + QRZ_LOGIN_ESPERA
        while time.time() < limite:
            page.wait_for_timeout(2000)
            if "/login" not in page.url.lower():
                if qrz_logueado(page):
                    print("QRZ: sesión guardada.")
                    return
                page.goto(QRZ_LOGIN, wait_until="domcontentloaded")
        raise RuntimeError("no se completó el login a tiempo")
    finally:
        ctx.close()


def qrz_sesion(p):
    """Abre el navegador invisible con sesión; si venció, pide el login manual antes."""
    ctx, page = qrz_abrir(p, visible=False)
    if qrz_logueado(page):
        return ctx, page
    ctx.close()  # el perfil no se puede abrir dos veces a la vez
    print("QRZ: no hay sesión o venció.")
    qrz_login_manual(p)
    ctx, page = qrz_abrir(p, visible=False)
    if not qrz_logueado(page):
        ctx.close()
        raise RuntimeError("sigue sin sesión después del login")
    return ctx, page


def qrz_preparar_importacion(page, ruta_adif):
    """Con sesión activa, deja el modal 'Import from ADI File' abierto con el archivo
    cargado. Devuelve el nombre del libro de destino."""
    # El modal no está en el DOM hasta que se elige en el menú Import/Export (import_dlg()).
    page.click("#dropdownMenuButton")
    page.locator(".dropdown-menu a", has_text="Import from ADI File").click()
    page.wait_for_selector("#importSubmitBtn", state="visible", timeout=15000)
    libro = page.locator("#importcontent .green").first.inner_text().strip()
    page.set_input_files("#upload_file", str(ruta_adif))
    return libro


def qrz_subir(cfg, ruta_adif, pendientes, registro, enviar=True):
    """Sube un ADIF al logbook de QRZ por la web (cuenta gratuita, sin API).

    QRZ sólo confirma la recepción del archivo ('uploaded successfully...') y lo
    procesa en segundo plano, así que no hay resultado por QSO: con esa confirmación
    se marcan todos como ok. QRZ descarta duplicados (call, modo, banda, fecha y minuto).
    Con enviar=False llena el formulario y no lo envía (para probar los locators).
    """
    from playwright.sync_api import sync_playwright

    print(f"QRZ: subiendo {len(pendientes)} QSOs ({ruta_adif.name})...")
    with sync_playwright() as p:
        ctx = None
        try:
            ctx, page = qrz_sesion(p)
            libro = qrz_preparar_importacion(page, ruta_adif)
            if libro != cfg["qrz_libro"]:
                estado, msg = PENDIENTE, f"libro de destino inesperado: '{libro}' (se esperaba '{cfg['qrz_libro']}'); no se envió"
            elif not enviar:
                print(f"QRZ: prueba OK, formulario listo para '{libro}'. No se envió.")
                page.wait_for_timeout(5000)
                return
            else:
                page.click("#importSubmitBtn")
                page.wait_for_selector("#resptext", state="visible", timeout=180000)
                msg = " ".join(page.locator("#resptext").inner_text().split())
                estado = OK if QRZ_OK in msg.lower() else PENDIENTE
        except Exception as e:  # noqa: BLE001 - cualquier falla del navegador deja los QSOs para reintentar
            estado, msg = PENDIENTE, f"error en el navegador: {type(e).__name__}: {str(e).splitlines()[0]}"
        finally:
            if ctx:
                ctx.close()

    for q in pendientes:
        marcar(registro["qsos"][clave_qso(q["campos"])], "qrz", estado, msg)
    guardar_registro(registro)
    print(f"  {estado} - {msg}")
    return estado == OK


# --------------------------------------------------------------------------- QRZ: correcciones

# Logbook > Awards: cada award lista los QSOs confirmados cuyo dato no coincide con lo
# que cargó el corresponsal. Se corrige el QSO (pestaña Receiver Info) con el valor del
# corresponsal y se vuelve a cargar el award, hasta que no quede tabla de errores.
QRZ_AWARDS = {
    "grid": "Grid Squared Award",
    "county": "United States Counties Award",
}
QRZ_MAX_CORRECCIONES = 200  # tope de seguridad por award y corrida
LBSHOW_RE = re.compile(r"lbshow\(\s*\d+\s*,\s*(\d+)\s*\)")
SAVE_OK_RE = re.compile(r"'nmval'\s*:\s*'((?:[^'\\]|\\.)*)'")


def qrz_abrir_award(cfg, page, nombre):
    """Va a Awards (si el menú del logbook no está a la vista, recarga primero), elige
    el libro y despliega el award. Devuelve el panel de contenido del award ya cargado."""
    menu = page.locator("li.menuItem", has_text="Awards")
    if not menu.is_visible():
        page.goto(QRZ_LOGBOOK, wait_until="domcontentloaded")
    menu.click()
    libros = page.locator("#listbooks")
    libros.wait_for(state="visible", timeout=30000)
    valor = libros.evaluate("(s, n) => [...s.options].find(o => o.text.trim() === n)?.value || ''", cfg["qrz_libro"])
    if not valor:
        raise RuntimeError(f"no está el libro '{cfg['qrz_libro']}' en Awards (ver QRZ_LIBRO en .env)")
    libros.select_option(valor)
    cabecera = page.locator("h3.awardContainerHeader", has_text=nombre)
    cabecera.wait_for(state="visible", timeout=30000)
    panel = page.locator("#" + cabecera.get_attribute("aria-controls"))
    cabecera.click()
    # Las estadísticas del award son lo último que carga el panel.
    panel.locator("#lbtab").first.wait_for(state="attached", timeout=90000)
    return panel


def qrz_errores_award(panel):
    """[{fecha, call, mio, corresponsal, id}] de la tabla 'not eligible' (vacía si no hay)."""
    errores = []
    for fila in panel.locator("table.awardErrorsTable tbody tr").all():
        celdas = [t.strip() for t in fila.locator("td").all_inner_texts()]
        m = LBSHOW_RE.search(fila.locator("button", has_text="View").get_attribute("onclick") or "")
        if len(celdas) >= 5 and m:
            errores.append({"fecha": celdas[0], "call": celdas[1], "mio": celdas[3],
                            "corresponsal": celdas[4], "id": m.group(1)})
    return errores


def qrz_editar_receptor(page, panel, err):
    """Abre el QSO del error en modo edición, en la pestaña Receiver Info."""
    panel.locator("button", has_text="Details").first.click()
    panel.locator(f"table.awardErrorsTable button[onclick*='{err['id']}']").click()
    page.locator("button:visible", has_text="Edit").first.click()
    page.locator("a[href='#tab_receiver']").click()
    page.wait_for_selector("#grid2", state="visible", timeout=30000)
    page.wait_for_load_state("networkidle")  # la pestaña dispara un lookup del indicativo
    call = page.input_value("#call2").strip().upper()
    if call != err["call"].upper():
        raise RuntimeError(f"se abrió {call} en vez de {err['call']}")


def qrz_poner_grid(page, valor):
    page.fill("#grid2", valor)
    page.press("#grid2", "Tab")  # dispara onchange (recalcula lat/lon)
    return page.input_value("#grid2").strip().upper() == valor.upper()


def qrz_poner_county(page, valor):
    """El county se elige en un popup buscador (/pas) que completa #county2 y su código."""
    with page.expect_popup() as info:
        page.locator("#all_county2 button", has_text="Set").click()
    popup = info.value
    popup.wait_for_load_state()
    popup.fill("input[name=query]", valor.rsplit(",", 1)[0].strip())
    popup.click("input[name=search]")
    popup.wait_for_load_state()
    for link in popup.locator("a[href^='javascript:save_ok']").all():
        m = SAVE_OK_RE.search(link.get_attribute("href") or "")
        if m and m.group(1).replace("\\'", "'") == valor:
            link.click()  # save_ok() completa el formulario y cierra el popup
            break
    else:
        popup.close()
        return False
    page.wait_for_function("v => document.getElementById('county2').innerText.trim() === v", arg=valor, timeout=10000)
    return True


def qrz_corregir_award(cfg, page, tipo, guardar):
    nombre = QRZ_AWARDS[tipo]
    poner = qrz_poner_grid if tipo == "grid" else qrz_poner_county
    intentados, corregidos, fallidos = set(), 0, []
    if not guardar:
        # Sólo lista: QRZ aplica los cambios del formulario aunque después se cancele,
        # así que en modo prueba no se abre ningún QSO en edición.
        for err in qrz_errores_award(qrz_abrir_award(cfg, page, nombre)):
            print(f"  {tipo} a corregir {err['fecha']} {err['call']}: '{err['mio']}' -> '{err['corresponsal']}'")
        return corregidos, fallidos
    for _ in range(QRZ_MAX_CORRECCIONES):
        panel = qrz_abrir_award(cfg, page, nombre)
        errores = [e for e in qrz_errores_award(panel) if e["id"] not in intentados]
        if not errores:
            break
        err = errores[0]
        intentados.add(err["id"])
        desc = f"{err['fecha']} {err['call']}: '{err['mio']}' -> '{err['corresponsal']}'"
        try:
            qrz_editar_receptor(page, panel, err)
            if not err["corresponsal"] or not poner(page, err["corresponsal"]):
                raise RuntimeError("no se pudo cargar el valor del corresponsal")
            page.click("#savebut")
            # Al guardar, QRZ vuelve a mostrar el QSO en modo vista (botón Edit).
            page.locator("button:visible", has_text="Edit").first.wait_for(timeout=30000)
            corregidos += 1
            print(f"  {tipo} corregido  {desc}")
        except Exception as e:  # noqa: BLE001 - se informa y se sigue con el próximo
            fallidos.append(desc)
            print(f"  {tipo} NO corregido {desc} - {type(e).__name__}: {str(e).splitlines()[0]}")
    else:
        print(f"  {tipo}: se alcanzó el tope de {QRZ_MAX_CORRECCIONES} correcciones en esta corrida.")
    # Un QSO guardado que vuelve a aparecer significa que QRZ no tomó la corrección.
    if intentados:
        restantes = {e["id"] for e in qrz_errores_award(qrz_abrir_award(cfg, page, nombre))}
        for e in intentados & restantes:
            print(f"  {tipo}: el QSO id={e} sigue figurando con error después de guardarlo.")
    return corregidos, fallidos


def qrz_corregir(cfg, guardar=True):
    """Corrige en QRZ los grids y counties que no coinciden con lo que cargó el corresponsal."""
    from playwright.sync_api import sync_playwright

    print(f"\nQRZ: {'corrigiendo' if guardar else 'listando (sin abrir ni editar)'} grids y counties...")
    with sync_playwright() as p:
        ctx = None
        try:
            ctx, page = qrz_sesion(p)
            for tipo in QRZ_AWARDS:
                corregidos, fallidos = qrz_corregir_award(cfg, page, tipo, guardar)
                print(f"  {tipo}: {corregidos} corregidos, {len(fallidos)} con problemas.")
        except Exception as e:  # noqa: BLE001
            print(f"  error en el navegador: {type(e).__name__}: {str(e).splitlines()[0]}")
        finally:
            if ctx:
                ctx.close()


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--estaciones", action="store_true", help="lista las estaciones (station_id) de Wavelog")
    ap.add_argument("--dry-run", action="store_true", help="no sube nada ni modifica el registro")
    ap.add_argument("--sin-qrz", action="store_true", help="no sube a QRZ")
    ap.add_argument("--sin-lda", action="store_true", help="no sube a LdA")
    ap.add_argument("--reintentar-rechazados", action="store_true",
                    help="vuelve a intentar los QSOs que un destino rechazó")
    ap.add_argument("--qrz-login", action="store_true",
                    help="abre una ventana para loguearte en QRZ a mano y guarda la sesión")
    ap.add_argument("--qrz-corregir", action="store_true",
                    help="sólo corrige en QRZ grids/counties que no coinciden (con --dry-run sólo los lista)")
    ap.add_argument("--todas", action="store_true", help="usa todas las estaciones (default)")
    ap.add_argument("--estacion", action="append", metavar="ID|NOMBRE|INDICATIVO",
                    help="sólo estas estaciones; repetible o separadas por coma")
    ap.add_argument("--excluir", action="append", metavar="ID|NOMBRE|INDICATIVO",
                    help="todas (o las de --estacion) menos estas; repetible o separadas por coma")
    args = ap.parse_args()
    if args.todas and args.estacion:
        ap.error("--todas y --estacion no se pueden combinar")

    cfg = cargar_config()

    if args.qrz_login:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            qrz_login_manual(p)
        return

    if args.qrz_corregir:
        qrz_corregir(cfg, guardar=not args.dry_run)
        return

    if args.estaciones:
        for e in wavelog_estaciones(cfg):
            print(f"station_id={e.get('station_id')}  {e.get('station_callsign')}  "
                  f"{e.get('station_profile_name')}  grid={e.get('station_gridsquare')}  "
                  f"activa={e.get('station_active')}")
        return

    estaciones = elegir_estaciones(wavelog_estaciones(cfg), _criterios(args.estacion), _criterios(args.excluir))
    registro = cargar_registro()
    qsos = wavelog_descargar(cfg, estaciones)

    # Filtrar por fecha de referencia y validar.
    validos, incompletos, vistos = [], [], set()
    for q in qsos:
        c = q["campos"]
        f = fecha_qso(c)
        if f is not None and f < cfg["desde"]:
            continue
        falta = faltantes(c)
        if falta:
            incompletos.append((c, falta))
            continue
        clave = clave_qso(c)
        if clave in vistos:
            continue
        vistos.add(clave)
        validos.append(q)

    reintentables = {PENDIENTE, RECHAZADO} if args.reintentar_rechazados else {PENDIENTE}
    activos = [d for d in DESTINOS if not getattr(args, f"sin_{d}")]

    def pendiente_en(q, destino):
        entrada = registro["qsos"].get(clave_qso(q["campos"]))
        return entrada is None or entrada[destino]["estado"] in reintentables

    a_procesar = [q for q in validos if any(pendiente_en(q, d) for d in activos)]

    print(f"\nDesde {cfg['desde']:%Y-%m-%d %H:%M} UTC: {len(validos)} QSOs válidos, "
          f"{len(incompletos)} incompletos, {len(a_procesar)} para subir.")
    if incompletos:
        print("\nIncompletos (se reintentan en la próxima corrida cuando los completes en Wavelog):")
        for c, falta in sorted(incompletos, key=lambda x: (x[0].get("QSO_DATE", ""), x[0].get("TIME_ON", ""))):
            print(f"  {c.get('QSO_DATE','?')} {c.get('TIME_ON','?')[:4]} {c.get('CALL','?'):<12} "
                  f"falta: {', '.join(falta)}")

    if not a_procesar:
        print("\nNada nuevo para subir.")
        if not args.dry_run:
            registro["ultima_ejecucion"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            guardar_registro(registro)
        return

    sello = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta = EXPORTS_DIR / f"wavelog_{sello}{'_dryrun' if args.dry_run else ''}.adi"
    escribir_adif(a_procesar, ruta, f"Exportado de Wavelog por wavelog_sync - {len(a_procesar)} QSOs")
    print(f"\nADIF unificado: {ruta}")

    if args.dry_run:
        print("Dry-run: no se sube nada ni se modifica el registro.")
        return

    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for q in a_procesar:
        c = q["campos"]
        entrada = registro["qsos"].setdefault(clave_qso(c), {
            "call": c.get("CALL"), "qso_date": c.get("QSO_DATE"), "time_on": c.get("TIME_ON"),
            "band": c.get("BAND"), "mode": c.get("MODE"), "exportado": ahora,
            **{d: {"estado": PENDIENTE, "mensaje": "", "fecha": None} for d in DESTINOS},
        })
        entrada.setdefault("adif", [])
        entrada["adif"].append(ruta.name)
    guardar_registro(registro)

    if "lda" in activos:
        lda_subir(cfg, [q for q in a_procesar if pendiente_en(q, "lda")], registro)

    if "qrz" in activos:
        pend_qrz = [q for q in a_procesar if pendiente_en(q, "qrz")]
        if pend_qrz:
            ruta_qrz = ruta
            if len(pend_qrz) != len(a_procesar):
                ruta_qrz = ruta.with_name(ruta.stem + "_qrz.adi")
                escribir_adif(pend_qrz, ruta_qrz, f"Pendientes para QRZ - {len(pend_qrz)} QSOs")
            if qrz_subir(cfg, ruta_qrz, pend_qrz, registro):
                qrz_corregir(cfg)

    registro["ultima_ejecucion"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    guardar_registro(registro)

    print("\nResumen:")
    for d in activos:
        conteo = {}
        for q in a_procesar:
            est = registro["qsos"][clave_qso(q["campos"])][d]["estado"]
            conteo[est] = conteo.get(est, 0) + 1
        print(f"  {d.upper()}: " + ", ".join(f"{k}={v}" for k, v in sorted(conteo.items())))


if __name__ == "__main__":
    main()
