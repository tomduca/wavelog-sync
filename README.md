# wavelog-sync

Sincroniza los QSOs nuevos de un log de [Wavelog](https://www.wavelog.org/) hacia
**QRZ.com** y **LogDeArgentina (LdA)**, y después corrige en QRZ los grids y counties que no
coinciden con lo que cargó el corresponsal.

Está pensado para una cuenta **gratuita** de QRZ (sin API de logbook): la subida y las
correcciones se hacen manejando la web de QRZ con Playwright, con una sesión iniciada a mano.

## Qué hace

1. **Descarga** el log de Wavelog por API (`api/get_contacts_adif`), una llamada por estación.
2. **Valida** cada QSO: fecha, hora, indicativo, grid, zona CQ y zona ITU obligatorios; para
   corresponsales de Argentina, además, la provincia (`STATE`). Los incompletos se informan y
   se reintentan solos en la próxima corrida, cuando los completes en Wavelog.
3. **Compara** contra un registro local y arma un ADIF con lo que falta subir a cada destino.
4. **Sube a LdA** por su API, un QSO por llamada (`subeqso2.php`).
5. **Sube a QRZ** importando el ADIF desde la web del logbook.
6. **Corrige en QRZ** (después de cada subida exitosa) los QSOs que los awards *Grid Squared*
   y *United States Counties* marcan como "not eligible", poniendo el valor del corresponsal.

Sólo se consideran QSOs posteriores a `SYNC_DESDE`, para no subir el log histórico.

## Requisitos

- macOS o Linux con `python3` (3.10 o más nuevo) y `bash`.
- Una instancia de Wavelog con una API key (alcanza con una de sólo lectura).
- Cuenta de LogDeArgentina y cuenta de QRZ.com (la gratuita sirve).

## Instalación

```bash
git clone <url-del-repo> wavelog-sync
cd wavelog-sync
cp .env.example .env      # y completarlo (ver Configuración)
./wavelog-sync.sh --estaciones
```

`wavelog-sync.sh` crea el entorno virtual (`.venv`), instala las dependencias y el Chromium de
Playwright la primera vez (y cuando cambia `requirements.txt`), y después corre
`wavelog_sync.py` con los argumentos que le pases.

## Configuración (`.env`)

| Variable | Obligatoria | Descripción |
|---|---|---|
| `WAVELOG_URL` | sí | URL base de Wavelog, la que precede a `/api/...`. Si tu instalación usa `index.php` en las URLs, incluilo: `http://mi-wavelog/index.php` |
| `WAVELOG_API_KEY` | sí | Wavelog → menú de usuario → API Keys |
| `SYNC_DESDE` | sí | Fecha/hora UTC desde la que se sincroniza, `AAAA-MM-DD HH:MM` |
| `LDA_USER`, `LDA_PASS` | para LdA | Credenciales de LogDeArgentina |
| `LDA_MICALL` | sí | Indicativo propio. Se usa si un QSO no trae `STATION_CALLSIGN` y para el nombre del libro de QRZ |
| `LDA_QSL_MSG` | no | Mensaje para la QSL de LdA |
| `QRZ_LIBRO` | no | Nombre exacto del logbook de QRZ como aparece en *Awards*. Default: `<LDA_MICALL> Log` |

QRZ no lleva usuario ni contraseña en `.env`: el login es manual (ver abajo).

## Uso

```bash
./wavelog-sync.sh                     # corrida completa: todas las estaciones, LdA + QRZ + correcciones
./wavelog-sync.sh --dry-run           # descarga, valida y arma el ADIF; no sube ni registra nada
./wavelog-sync.sh --sin-qrz           # sólo LdA
./wavelog-sync.sh --sin-lda           # sólo QRZ (y sus correcciones)
./wavelog-sync.sh --estaciones        # lista las estaciones de Wavelog
./wavelog-sync.sh --qrz-login         # abre una ventana para loguearse en QRZ
./wavelog-sync.sh --qrz-corregir      # sólo corrige grids/counties en QRZ
./wavelog-sync.sh --qrz-corregir --dry-run   # lista lo que corregiría, sin tocar nada
./wavelog-sync.sh --reintentar-rechazados    # reintenta QSOs que un destino rechazó por datos
```

### Elegir estaciones

Por defecto se sincronizan **todas** las estaciones de Wavelog. Se pueden elegir o excluir por
`station_id`, nombre del perfil o indicativo (sin distinguir mayúsculas); los criterios se
repiten o se separan por coma. Un criterio que no coincide con ninguna estación corta la
ejecución, para no sincronizar algo distinto de lo pedido por un error de tipeo.

```bash
./wavelog-sync.sh --estacion Casa
./wavelog-sync.sh --estacion 1,11 --estacion LU0XXX/M    # LU0XXX/M: todas las de ese indicativo
./wavelog-sync.sh --excluir "Contest"
./wavelog-sync.sh --estacion LU0XXX/M --excluir 11
```

## QRZ: sesión

La primera vez (y cuando la sesión vence) hay que loguearse a mano:

```bash
./wavelog-sync.sh --qrz-login
```

Se abre una ventana de Chromium en el login de QRZ; al entrar (con 2FA si lo tenés; conviene
marcar *trust this device*) la ventana se cierra y la sesión queda guardada en el perfil
`estado/qrz_navegador`. Las corridas siguientes usan esa sesión con el navegador invisible.
Si detectan que venció, vuelven a abrir la ventana de login y siguen solas.

## QRZ: correcciones de grid y county

Después de cada subida exitosa a QRZ (o con `--qrz-corregir`), para cada award:

1. Logbook → *Awards* → se elige el libro → se despliega *Grid Squared Award* o
   *United States Counties Award*.
2. Si hay QSOs "not eligible", se abre el primero, *Edit* → *Receiver Info*, y se pone el
   valor que cargó el corresponsal (el grid se escribe; el county se elige en el buscador de
   QRZ, sólo si hay una coincidencia exacta).
3. *Save Record*, de vuelta a *Awards*, y así hasta que no quede tabla de errores.

A tener en cuenta:

- Se pisa tu dato con el del corresponsal, tal como lo muestra QRZ (en grids suele ser de
  4 caracteres, así que se pierde precisión si tenías 6).
- Los errores aparecen recién cuando el corresponsal confirma, así que en general se corrigen
  en corridas posteriores a la subida.
- Las correcciones se hacen sólo en QRZ; Wavelog no se modifica.
- **QRZ guarda los cambios del formulario de edición aunque después se cancele.** Por eso el
  `--dry-run` de las correcciones sólo lista, sin abrir ningún QSO.

## Estado y reintentos

El registro local (`estado/registro.json`) guarda, por QSO y por destino, uno de estos estados:

| Estado | Significado | Próxima corrida |
|---|---|---|
| `pendiente` | no se intentó o falló por algo transitorio (red, navegador, sesión) | se reintenta |
| `ok` | subido; incluye "duplicado" informado por el destino | no se toca |
| `rechazado` | el destino lo rechazó por datos | sólo con `--reintentar-rechazados` |

Cada destino es independiente: si falla LdA, la próxima corrida reintenta LdA junto con los
QSOs nuevos, y a QRZ le sube sólo lo que le falta. QRZ no informa resultado por QSO (procesa
el archivo en segundo plano); se marca `ok` cuando confirma la recepción.

Un QSO se identifica por indicativo + fecha + hora (al minuto) + banda + modo.

Los ADIF generados quedan en `exports/`.

## Archivos

| Ruta | Contenido | En git |
|---|---|---|
| `wavelog_sync.py` | el programa | sí |
| `wavelog-sync.sh` | lanzador con manejo del venv | sí |
| `.env.example` | plantilla de configuración | sí |
| `.env` | tu configuración y credenciales | **no** |
| `estado/` | registro de subidas y perfil del navegador con la sesión de QRZ | **no** |
| `exports/` | ADIF generados (contienen tu log) | **no** |

## Correr periódicamente

En macOS se puede usar `cron` o `launchd`; por ejemplo, con `crontab -e`:

```cron
0 * * * * /ruta/a/wavelog-sync/wavelog-sync.sh >> /ruta/a/wavelog-sync/estado/cron.log 2>&1
```

Si la sesión de QRZ vence durante una corrida automática, se abre la ventana de login y la
corrida espera hasta 10 minutos; si nadie se loguea, los QSOs quedan `pendiente` para QRZ.

## Limitaciones

- Depende de la estructura de la web de QRZ: si QRZ la cambia, la subida o las correcciones
  pueden dejar de funcionar y hay que ajustar los selectores en `wavelog_sync.py`.
- La API de Wavelog no filtra por fecha: se descarga el log completo en cada corrida y se
  filtra localmente (unos pocos MB, alrededor de un segundo).
