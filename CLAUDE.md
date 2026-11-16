# wavelog-sync — mapa del código

Sincroniza QSOs de Wavelog hacia **LogDeArgentina (LdA)** (API HTTP) y **QRZ.com** (Playwright,
cuenta gratuita sin API), y corrige en QRZ grid/county/DXCC según los Awards. Repo público (MIT):
nunca commitear `.env`, `estado/`, `exports/` ni datos personales (indicativo, nombres de estación,
book id de QRZ, IP de LAN).

## Archivos

| Archivo | Qué es |
|---|---|
| `wavelog_sync.py` | Todo el programa (un solo módulo, ~760 líneas) |
| `wavelog-sync.sh` / `.command` / `.bat` | Lanzadores (crean `.venv`, instalan deps y Chromium, ejecutan) |
| `.env.example` | Variables de config; `.env` real no va a git |
| `estado/registro.json` | Registro local: estado por QSO y por destino (no va a git) |
| `estado/qrz_navegador/` | Perfil persistente de Playwright con la sesión de QRZ (no va a git) |
| `exports/*.adi` | ADIF generados en cada corrida (no va a git) |

## Secciones de `wavelog_sync.py` (separadas por `# ---- nombre`)

- **config** — `cargar_config()`: lee `.env` → dict `cfg` (`wavelog_url`, `wavelog_key`, `desde`, `lda_*`, `qrz_libro`).
- **ADIF** — `parsear_adif()` (tolera largos en chars o bytes UTF-8 vía `_leer_valor`), `escribir_adif()`,
  `clave_qso()` = `CALL|QSO_DATE|HHMM|band|MODE` (clave del registro), `fecha_qso()`.
- **validación** — `faltantes()`: obligatorios fecha, hora, call, CQZ, ITUZ; STATE si es Argentina (`es_argentina`).
- **Wavelog** — `wavelog_estaciones()` (`api/station_info`), `elegir_estaciones()` (`--estacion/--excluir`),
  `wavelog_descargar()` (`api/get_contacts_adif`, `fetchfromid=0`, siempre descarga completa).
- **registro** — `cargar_registro()`, `guardar_registro()` (escritura atómica), `marcar()`.
  Estados por destino: `pendiente` (se reintenta), `ok`, `rechazado`. Cada destino guarda `huella()` de los datos
  enviados; un rechazado se reintenta solo si la huella cambió (corregido en Wavelog; campos de QSL/subidas
  `VOLATIL_RE` no cuentan) o con `--reintentar-rechazados`. Sin huella (registros viejos) = sin cambios.
- **LdA** — `lda_subir()` elige la API: `lda_subir_qso_adif()` (POST ADIF a `subeqso_l40.php` con `LDA_API_KEY`;
  estado por cabecera `X-LdA-Status` y código de error) o `lda_subir_qso()` (GET a `subeqso2.php` con user/pass).
  Si la ADIF da 404 cambia a la vieja. Ambas devuelven `(estado, mensaje, cortar)`; `cortar` = error de cuenta
  (AUTH, ACCOUNT_GRID). `lda_adif()` arma un ADIF mínimo (`LDA_CAMPOS_ADIF`), `lda_modo()` (MFSK/PSK→SUBMODE,
  PSK31→BPSK31), `lda_prop()` (sólo propagaciones de `LDA_PROP`). 0.5 s entre QSOs.
- **QRZ** — `qrz_abrir/qrz_logueado/qrz_login_manual/qrz_sesion` (sesión persistente, login manual),
  `qrz_preparar_importacion()` + `qrz_subir()` (modal "Import from ADI File"; verifica nombre del libro; sin resultado por QSO).
- **QRZ: correcciones** — `QRZ_AWARDS` (grid, county, dxcc). `qrz_abrir_award()`, `qrz_errores_award()`,
  `qrz_editar_receptor()`, `qrz_poner_grid/county/dxcc()`, `qrz_corregir_award()`, `qrz_corregir()`.
  DXCC: se saltean los NON-DXCC (0); `countryPick()` pisa grid/lat/lon/zonas y se restauran.
- **main** — argparse → filtra por `SYNC_DESDE`, valida, deduplica, arma ADIF unificado, sube LdA, sube QRZ,
  y si QRZ subió OK corre `qrz_corregir()`. Imprime resumen y al final `imprimir_no_subidos()`
  (incompletos en Wavelog + rechazados por un destino), en todos los caminos (nada nuevo, dry-run, completa).

## Reglas que no se deducen del código

- QRZ aplica cambios del formulario de edición **aunque se cancele**: nunca abrir un QSO en edición "para probar".
  `--qrz-corregir --dry-run` sólo lista.
- Wavelog NO sube a QRZ; no confiar en `QRZCOM_QSO_UPLOAD_STATUS`.
- No subir nada real a QRZ/LdA sin OK explícito del usuario.
- Doc de la API de LdA: https://lda.ar/?page_id=25 (el contenido real está en el iframe
  https://lda.ar/php/desarrolladores/api_desarrolladores.html).
