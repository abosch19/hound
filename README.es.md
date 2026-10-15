# Hound

[English](README.md) · **Español** · [Català](README.ca.md)

Buscador semántico para la carpeta compartida de un Windows Server o de un servidor Linux (Samba), consultable desde **Claude Code** por **MCP**.

Hound indexa los ficheros de la carpeta compartida y permite encontrarlos por su significado ("el contrato con el proveedor de limpieza", "la factura de marzo de Pérez", "el presupuesto de la reforma"), no solo por su nombre. Funciona entero dentro del servidor:

- **Nada sale del servidor al indexar.** El texto se extrae y los embeddings se calculan en local, en CPU.
- **Cada búsqueda respeta los permisos de fichero del usuario que pregunta** (ACL de NTFS en Windows; propietario, grupo, bits de modo y ACL POSIX en Linux). Si alguien no puede abrir un fichero en el disco, tampoco ve su nombre ni ningún fragmento.
- **Nunca modifica la carpeta compartida.** Solo la lee.

```
 Equipo del trabajador (VPN)                      Windows Server / Linux
┌────────────────────────┐   HTTP + token   ┌──────────────────────────────────────────┐
│ Claude Code            │ ───────────────► │ Servicio "Hound"                          │
│  └─ MCP "hound"        │     /mcp         │  ├─ Servidor MCP (búsqueda + permisos)    │
└────────────────────────┘                  │  └─ Indexador (prioridad baja)            │
                                            │        │ lee            │ escribe          │
                                            │   D:\Compartido    índice SQLite          │
                                            └──────────────────────────────────────────┘
```

## Qué indexa

| Tipo | Extensiones | Qué se extrae |
|---|---|---|
| PDF | `.pdf` | Texto por página; **OCR** de las páginas escaneadas |
| Imágenes | `.jpg .png .tif .bmp .webp` | Texto por **OCR** (documentos fotografiados, escaneos, rótulos) |
| AutoCAD | `.dxf`, `.dwg` | Textos, atributos de bloques (cajetines), cotas y capas. Los DWG necesitan [ODA File Converter](#ficheros-dwg-de-autocad); sin él se indexan por nombre |
| Word | `.docx .docm .odt .rtf` | Párrafos, tablas, encabezados |
| Excel | `.xlsx .xlsm .xls .ods .csv` | Filas por hoja, con la cabecera como contexto |
| PowerPoint | `.pptx .pptm .odp` | Texto y notas por diapositiva |
| Correo | `.msg .eml` | Cabecera, cuerpo y **adjuntos** (PDF, Word, Excel…) |
| Texto | `.txt .md .log` | Párrafos |
| Otros | `.doc .ppt .zip .mp4`… | Solo nombre y carpeta |

**Todos** los ficheros se pueden encontrar también por su nombre y su carpeta, incluso cuando no se puede leer su contenido.

Cada resultado indica **dónde** está la coincidencia: "página 3", "hoja Gastos, filas 2–40", "diapositiva 5", "adjunto factura.pdf, página 1", "bloque CAJETIN"…

## Requisitos

- **Windows:** Windows Server 2016 o posterior, de 64 bits.
- **Linux:** una distribución de 64 bits (x86_64 o aarch64) con systemd: Debian/Ubuntu, RHEL/Rocky/Alma (con EPEL), Fedora, openSUSE o Arch. La carpeta compartida suele servirse con Samba.
- Acceso a internet durante la instalación.
- Unos 2 GB de disco para el programa y el modelo, más el índice: unos 4–5 KB por fragmento. Con 1 millón de fragmentos son unos 5 GB.
- RAM para los vectores en memoria: unos 3 KB por fragmento. Con 1 millón son unos 3 GB, o la mitad con `memory_dtype = "float16"`.
- No necesita GPU.

## Instalación

### Windows Server

En el servidor, abre **PowerShell como administrador** y ejecuta:

```powershell
irm https://raw.githubusercontent.com/abosch19/hound/main/install.ps1 | iex
```

Si el servidor solo tiene un recurso compartido, lo detecta solo; si tiene varios, pregunta cuál. Para indicar los parámetros, sobre todo la **red de la VPN** que podrá conectarse:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/abosch19/hound/main/install.ps1))) -SharePath D:\Compartido -AllowFrom 10.8.0.0/24
```

Si prefieres descargarlo primero con `curl`:

```powershell
curl.exe -fsSL https://raw.githubusercontent.com/abosch19/hound/main/install.ps1 -o install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1 -SharePath D:\Compartido -AllowFrom 10.8.0.0/24
```

| Parámetro | Por defecto | Qué es |
|---|---|---|
| `-SharePath` | se detecta | Ruta local de la carpeta compartida |
| `-AllowFrom` | subred local | Red(es) que pueden conectar al MCP, p. ej. la de la VPN. Separadas por comas |
| `-Port` | `8765` | Puerto del MCP |
| `-ShareName` / `-Unc` | se detectan | Nombre del recurso SMB y ruta `\\SERVIDOR\Recurso` que se muestra en los resultados |
| `-OcrLanguages` | `spa,eng` | Idiomas de OCR (p. ej. `spa,eng,cat`) |
| `-Version` | `main` | Rama o etiqueta de GitHub |
| `-NoOcr` | | No instala Tesseract |

El instalador hace lo siguiente:

1. Instala `uv` y un Python 3.12 propio en `C:\Program Files\Hound`, sin tocar el Python del sistema.
2. Instala Hound y sus dependencias.
3. Instala Tesseract con los idiomas de OCR.
4. Descarga el modelo de embeddings, una sola vez.
5. Crea `C:\ProgramData\Hound\config.toml`.
6. Registra el servicio **Hound**: arranque automático y reinicio si falla.
7. Activa en Windows el soporte de rutas largas (`LongPathsEnabled`).
8. Abre el puerto en el firewall solo para `-AllowFrom`.

### Linux

En el servidor, como root (o con `sudo`):

```bash
curl -fsSL https://raw.githubusercontent.com/abosch19/hound/main/install.sh | sudo bash -s -- --share /srv/shared --allow-from 10.8.0.0/24
```

Sin `--share`, se leen los recursos de Samba con `testparm`: si hay uno solo se usa, y si hay varios pregunta cuál.

| Parámetro | Por defecto | Qué es |
|---|---|---|
| `--share` | se detecta | Ruta local de la carpeta compartida |
| `--allow-from` | subred local | Red(es) que pueden conectar al MCP, p. ej. la de la VPN. Separadas por comas |
| `--port` | `8765` | Puerto del MCP |
| `--share-name` / `--unc` | se detectan | Nombre del recurso de Samba y ruta `\\SERVIDOR\Recurso` que se muestra en los resultados |
| `--ocr-languages` | `spa,eng` | Idiomas de OCR (p. ej. `spa,eng,cat`) |
| `--version` | `main` | Rama o etiqueta de GitHub |
| `--no-ocr` / `--no-firewall` / `--no-service` | | No instala Tesseract, ni toca el firewall, ni crea el servicio de systemd |

El instalador hace lo siguiente:

1. Instala `uv` y un Python 3.12 propio en `/opt/hound`, sin tocar el Python del sistema.
2. Instala Hound y sus dependencias.
3. Instala Tesseract y los idiomas de OCR con el gestor de paquetes del sistema (apt, dnf/yum, zypper o pacman).
4. Descarga el modelo de embeddings, una sola vez.
5. Crea `/etc/hound/config.toml` (con `permissions.mode = "posix"`); el índice, los tokens y los registros van a `/var/lib/hound`, legible solo por root.
6. Registra el servicio **hound** de systemd: arranque automático y reinicio si falla. Corre como root para poder leer toda la carpeta, pero con sus capacidades reducidas a la lectura de ficheros (`CAP_DAC_READ_SEARCH`) y el resto del sistema en solo lectura.
7. Sube `fs.inotify.max_user_watches` si hace falta, para que la vigilancia de cambios pueda cubrir carpetas grandes.
8. Abre el puerto solo para `--allow-from` con **ufw** o **firewalld**, el que esté activo. Si no hay ninguno, avisa de que debes restringir el puerto tú.
9. Instala la orden `hound` en `/usr/local/bin` (úsala con `sudo`).

### Seguir la indexación

Al terminar la instalación, el servidor MCP ya responde y el índice se construye en segundo plano. El servidor MCP es lo primero que arranca: el indexador solo empieza cuando el puerto ya está abierto, así que puedes seguir el progreso desde el principio.

- Desde el servidor: `hound status` (en Linux, `sudo hound status`).
- Desde Claude Code: pregunta *"¿cómo va la indexación de Hound?"*. La herramienta `index_status` devuelve la fase (escaneando, indexando, en pausa, al día, detenido), el porcentaje, los ficheros pendientes y con error, los ficheros por hora, el tiempo estimado que queda y cuántos ficheros puedes ver. Solo devuelve recuentos, nunca rutas.

> **Indexación inicial.** Con muchos ficheros (sobre todo PDFs escaneados e imágenes con OCR), la primera indexación puede durar horas o días. En horario laboral el indexador va despacio, con prioridad baja de CPU y disco. Si prefieres que solo trabaje de noche, pon `busy_mode = "pause"` en `config.toml`. Después solo procesa los cambios.

### Actualizar

Vuelve a ejecutar el mismo comando de instalación (`install.ps1` o `install.sh`). Se conservan la configuración, el índice y los tokens.

### Desinstalar

En el servidor, desde una consola de administrador (en Linux, con `sudo`):

```bash
hound uninstall
```

Pide confirmación y conserva el índice, los tokens y la configuración; añade `--purge` para borrarlos también, y `-y` para no preguntar. En Windows sigue en una ventana nueva. También puedes usar directamente los scripts:

En Windows:

```powershell
irm https://raw.githubusercontent.com/abosch19/hound/main/uninstall.ps1 | iex
```

Conserva los datos en `C:\ProgramData\Hound`. Para borrarlos también:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/abosch19/hound/main/uninstall.ps1))) -Purge
```

En Linux (se conservan los datos de `/etc/hound` y `/var/lib/hound`, salvo que añadas `--purge`):

```bash
curl -fsSL https://raw.githubusercontent.com/abosch19/hound/main/uninstall.sh | sudo bash -s -- --purge
```

Tesseract no se desinstala en ninguno de los dos sistemas, porque otros programas pueden usarlo.

## Añadir un usuario y conectar Claude Code

Cada trabajador usa un **token personal** asociado a **su cuenta en el servidor** (Windows, o Linux/Samba). Las búsquedas se hacen con los permisos de esa cuenta. En el servidor, como administrador (en Linux, con `sudo`):

```powershell
hound token create maria --host SERVIDOR
```

La cuenta puede ser `maria`, `EQUIPO\maria` o `DOMINIO\maria`: local si el servidor es un grupo de trabajo, o del dominio si hay Active Directory. En Linux sirve cualquier cuenta que el sistema conozca (`getent passwd maria`), incluidas las del dominio mediante winbind o sssd. La orden muestra el token **una sola vez** y el comando exacto que la persona debe ejecutar en su ordenador, con la VPN conectada:

```bash
claude mcp add --transport http --scope user hound http://SERVIDOR:8765/mcp --header "Authorization: Bearer hound_..."
```

Después, en Claude Code basta con pedir cosas como *"busca en la carpeta compartida el contrato con el proveedor de limpieza y dime cuándo vence"*.

Hound expone estas herramientas:

| Herramienta | Qué hace |
|---|---|
| `search_files(query, folder?, file_type?, date_from?, date_to?, limit?)` | Búsqueda híbrida. Devuelve ruta UNC, ubicación, fragmento resaltado, fecha y puntuación |
| `read_document(path, start?, end?)` | Texto extraído del fichero por tramos, sin cargarlo entero |
| `list_folder(path?)` | Subcarpetas y ficheros visibles para el usuario |
| `recent_files(limit?, folder?)` | Últimos ficheros modificados |
| `index_status()` | Cómo va la indexación: fase, progreso, ficheros pendientes, ritmo y tiempo estimado que queda |

Con `share.unc` configurado, los resultados incluyen también `file_url` y `folder_url` (`file://SERVIDOR/Recurso/...`), y Claude los muestra como enlaces que se pueden pulsar: en Windows el primero abre el fichero con su programa y el segundo abre el Explorador en su carpeta. El ordenador del usuario debe poder acceder a la carpeta compartida (red de la oficina o VPN).

Otras órdenes para gestionar los tokens:

```powershell
hound token list
hound token revoke maria   # todos los tokens de maria
hound token revoke 7       # solo el token con id 7
```

Un token revocado deja de funcionar en menos de 30 segundos.

> **Por qué Claude Code y no los conectores de claude.ai.** Los conectores remotos de claude.ai se llaman desde los servidores de Anthropic, que no pueden llegar a un servidor que solo es accesible por VPN. Claude Code se conecta desde el ordenador del usuario, que sí está en la VPN. Hound no debe exponerse a internet.

## Permisos

### Windows (NTFS)

- Al indexar, Hound guarda el descriptor de seguridad NTFS de cada fichero: propietario, grupo y ACL.
- Al buscar, comprueba con la API **Authz** de Windows si la cuenta del token puede leer cada fichero. Tiene en cuenta grupos, grupos anidados, herencia y permisos de "Denegar", igual que el propio Windows.
- La comprobación se hace **antes** de ordenar los resultados, así que un fichero sin permiso nunca aparece: ni en búsquedas, ni en `read_document`, ni en `list_folder`, ni en `recent_files`.
- Si `share.name` está configurado, se aplican también los permisos del recurso compartido (pestaña "Compartir").
- Ante cualquier duda (fichero sin descriptor, error de Windows) el acceso se **deniega**.
- Los cambios de permisos en el disco se aplican en el siguiente reescaneo: enseguida si los detecta la vigilancia de cambios y, como mucho, cada `rescan_minutes`.

Otros puntos a tener en cuenta:

- **Cuenta del servicio.** El servicio corre como `SYSTEM`, que debe poder leer la carpeta compartida (es lo habitual). `hound doctor` lo comprueba.
- **Servidor en dominio.** Para calcular los grupos de un usuario del dominio, la cuenta de equipo del servidor debe poder leer el atributo `tokenGroupsGlobalAndUniversal`. Normalmente ya puede; si `hound search --as DOMINIO\usuario` falla, añade el equipo al grupo **Windows Authorization Access Group** del dominio.
- **Modo sin permisos.** Con `permissions.mode = "none"` todos los usuarios con token ven todo el índice. Úsalo solo si de verdad todo el mundo puede ver todo.

### Linux (POSIX)

- Al indexar, Hound guarda el propietario, el grupo, los bits de modo y la ACL POSIX (`setfacl`) de cada fichero, además de los de cada carpeta desde la raíz del recurso hasta él.
- Al buscar, aplica la misma comprobación que hace el kernel cuando Samba abre un fichero en nombre del usuario: permiso de lectura sobre el fichero y de paso (`x`) en todas las carpetas del camino. Los grupos se resuelven con NSS, así que funcionan las cuentas locales, de winbind y de sssd, incluidas las entradas de ACL de usuario y de grupo con nombre y la máscara de la ACL.
- Todo lo demás funciona como en Windows: se comprueba antes de ordenar, se deniega ante la duda y los cambios se recogen en el siguiente reescaneo.
- No cubre: las reglas exclusivas de Samba en `smb.conf` (`valid users`, `read list`, `force user`…) ni las ACL de Windows guardadas por `vfs_acl_xattr` que no se reflejen en las ACL POSIX. Si dependes de ellas, usa exclusiones o comprueba con `hound search --as`.

Para comprobar qué vería un usuario concreto:

```powershell
hound search "nóminas de marzo" --as maria
```

## Carpetas excluidas

Lo que Claude lee con estas herramientas se envía a Anthropic para generar la respuesta. Por eso, las carpetas que no deben salir del servidor (nóminas, RR. HH., datos de salud…) se excluyen del índice en `config.toml`:

```toml
[index]
exclude = ["RRHH", "Administracion/Nominas"]
```

Lo excluido no se indexa ni se puede consultar, y lo que ya estuviera indexado se borra del índice en el siguiente escaneo. Las rutas son relativas a la carpeta compartida y no distinguen mayúsculas.

## Ficheros DWG de AutoCAD

DWG es un formato cerrado. Para leer su contenido (textos, cajetines, capas):

1. Descarga e instala **ODA File Converter**, gratuito, desde https://www.opendesign.com/guestfiles/oda_file_converter. No lo incluye el instalador porque su licencia exige aceptarla.
2. Reinicia el servicio con `Restart-Service Hound` (Linux: `sudo systemctl restart hound`). Hound lo detecta en `C:\Program Files\ODA\` en Windows, o como `ODAFileConverter` en el `PATH` en Linux (el paquete `.deb`/`.rpm` lo instala en `/usr/bin`), y reprocesa automáticamente los DWG que antes se indexaron solo por nombre.

Si está en otra ruta, indícala en `[cad] oda_converter`.

## Operación

| Orden | Para qué |
|---|---|
| `hound status` | Fase del indexador, progreso, ficheros pendientes y con error, ritmo y tiempo estimado que queda (igual que la herramienta `index_status`) |
| `hound errors` | Ficheros que no se pudieron leer y por qué (PDF con contraseña, ficheros dañados…) |
| `hound log --user maria` | Registro de consultas: quién buscó qué y cuándo |
| `hound doctor` | Comprueba carpeta, modelo, Tesseract, ODA y permisos |
| `hound reindex --errors` | Reintenta los ficheros con error |
| `hound reindex --path "Obras\2024"` | Reindexa una carpeta o un fichero |
| `hound reindex --all` | Rehace el índice entero (p. ej. tras cambiar de modelo). Conserva tokens y registro |
| `hound backup D:\Copias` | Copia de seguridad en caliente del índice |
| `Restart-Service Hound` / `sudo systemctl restart hound` | Aplica cambios de `config.toml` |

Los registros están en `C:\ProgramData\Hound\logs` en Windows, y en `/var/lib/hound/logs` y `journalctl -u hound` en Linux. En Linux, ejecuta estas órdenes con `sudo`.

### Copia de seguridad y restauración

El índice (`C:\ProgramData\Hound\index.sqlite`, o `/var/lib/hound/index.sqlite` en Linux) incluye los usuarios, los tokens y el registro de consultas. Para hacer la copia sin parar el servicio:

```powershell
hound backup D:\Copias
```

Puedes programarla con el Programador de tareas (en Linux, `sudo hound backup /backups` desde cron o un temporizador de systemd). Para restaurar:

1. `Stop-Service Hound` (Linux: `sudo systemctl stop hound`).
2. Copia el fichero encima de `index.sqlite` y borra `index.sqlite-wal` e `index.sqlite-shm` si existen.
3. `Start-Service Hound` (Linux: `sudo systemctl start hound`).

Si se pierde el índice, no se pierde nada de la carpeta compartida: basta con `hound reindex --all` y volver a crear los tokens.

## Configuración

El fichero es `C:\ProgramData\Hound\config.toml` en Windows y `/etc/hound/config.toml` en Linux. Tras cambiarlo, ejecuta `Restart-Service Hound` o `sudo systemctl restart hound`.

| Sección | Opción | Por defecto | Descripción |
|---|---|---|---|
| `share` | `path` | | Ruta local de la carpeta compartida |
| | `unc` | | Ruta UNC que se muestra en los resultados |
| | `name` | | Recurso SMB cuyos permisos se aplican también |
| `server` | `host`, `port` | `0.0.0.0`, `8765` | Dónde escucha el MCP |
| | `tls_cert`, `tls_key` | | Rutas PEM para servir HTTPS |
| `index` | `exclude` | `[]` | Carpetas excluidas |
| | `exclude_names` | papelera, `~$*`, `Thumbs.db`… | Nombres ignorados (glob) |
| | `extensions` | ver arriba | Extensiones que se indexan |
| | `max_file_mb` | `300` | Por encima, solo por nombre |
| | `rescan_minutes` | `360` | Reescaneo completo periódico |
| | `watch` | `true` | Vigilancia de cambios en tiempo real |
| | `extract_workers` | `2` | Procesos de extracción en paralelo |
| `index.schedule` | `busy_hours`, `busy_days` | `08:00-19:00`, `mon-fri` | Horario laboral |
| | `busy_mode` | `slow` | `slow`, `pause` o `normal` |
| `ocr` | `enabled`, `languages`, `dpi` | `true`, `spa+eng`, `300` | OCR con Tesseract |
| `cad` | `oda_converter` | se detecta | Ruta a `ODAFileConverter.exe` |
| `model` | `repo`, `file`, `dim` | EmbeddingGemma-300M int8, 768 | Modelo de embeddings ONNX |
| | `threads` | la mitad de los núcleos | Hilos de CPU para los embeddings |
| `search` | `memory_dtype` | `float32` | `float16` usa la mitad de RAM y es algo más lento |
| `permissions` | `mode` | `ntfs` | `ntfs` o `none` |

Sobre HTTPS: dentro de la VPN el tráfico ya va cifrado. Si aun así quieres usar HTTPS con un certificado propio, los usuarios deben configurar `NODE_EXTRA_CA_CERTS` con la CA para que Claude Code lo acepte.

## Cómo funciona

- **Índice:** un único SQLite en modo WAL, al estilo de [DigUp](https://github.com/ARahim3/DigUp). El indexador es el único que escribe; el servidor solo lee. Tablas:
  - `files`: ruta, tipo, tamaño, fecha, estado y descriptor de seguridad;
  - `segments`: fragmentos con su ubicación, el texto y el vector en float16;
  - `segments_fts`: índice de palabras clave FTS5, sin tildes;
  - `security_descriptors`: deduplicados, porque casi todos los ficheros heredan los permisos de su carpeta;
  - `users`, `tokens` y `query_log`.
- **Modelo:** [EmbeddingGemma-300M](https://huggingface.co/onnx-community/embeddinggemma-300m-ONNX), cuantizado int8, con ONNX Runtime en CPU. Es multilingüe, con buena calidad en español, y dos veces más ligero que bge-m3. Se descarga una vez y funciona sin conexión.
- **Búsqueda híbrida:**
  1. Similitud coseno contra todos los vectores en memoria (numpy, por bloques).
  2. En paralelo, FTS5 con BM25 para las palabras clave.
  3. Las dos listas se combinan con *Reciprocal Rank Fusion* y se agrupan por fichero.

  Con 1 millón de fragmentos, la parte vectorial tarda unos 120 ms.
- **Indexado incremental:**
  - un reescaneo periódico compara el tamaño, la fecha y los permisos de cada fichero;
  - la vigilancia de cambios (`ReadDirectoryChangesW` en Windows, inotify en Linux) reacciona al momento;
  - la extracción corre en procesos aparte con tiempo máximo por fichero, y un fichero que falla se registra y no detiene nada.

## Limitaciones conocidas

- `.doc` y `.ppt` antiguos (formato binario) se indexan solo por nombre.
- Las imágenes se buscan por el texto que contienen (OCR), no por lo que muestran. La búsqueda de fotos por contenido y el audio quedan para una fase posterior.
- Las etiquetas de ubicación y los mensajes del servidor están en español.
- Las rutas se comparan sin distinguir mayúsculas, como en Windows. En Linux, dos ficheros de la misma carpeta cuyos nombres solo se diferencian en las mayúsculas se indexan como uno.

## Desarrollo

```bash
uv venv && uv pip install -e ".[dev]"
python -m pytest
```

- Los tests de permisos con ACL reales (`tests/test_windows_acl.py`) crean usuarios locales: solo se ejecutan en Windows con `HOUND_WIN_ACL_TESTS=1`. La CI de GitHub lo hace en `windows-latest`, junto con una instalación completa con `install.ps1`.
- Los tests de permisos POSIX (`tests/test_posix_acl.py`) se ejecutan en Linux y macOS; el de ACL reales necesita `setfacl`. La CI también hace una instalación completa en `ubuntu-latest` con `install.sh`, comprueba que una carpeta sin permiso sigue oculta y desinstala.
- `HOUND_BENCH=1` activa la prueba de rendimiento con 1 millón de fragmentos.

El encargo original está en [docs/PROMPT.md](docs/PROMPT.md).

## Licencia

MIT
