# Hound

[English](README.md) · [Español](README.es.md) · **Català**

Cercador semàntic per a la carpeta compartida d'un Windows Server o d'un servidor Linux (Samba), consultable des de **Claude Code** per **MCP**.

Hound indexa els fitxers de la carpeta compartida i permet trobar-los pel seu significat ("el contracte amb el proveïdor de neteja", "la factura de març d'en Pérez", "el pressupost de la reforma"), no només pel nom. Funciona sencer dins del servidor:

- **No surt res del servidor en indexar.** El text s'extreu i els embeddings es calculen en local, a la CPU.
- **Cada cerca respecta els permisos de fitxer de l'usuari que pregunta** (ACL de NTFS a Windows; propietari, grup, bits de mode i ACL POSIX a Linux). Si algú no pot obrir un fitxer al disc, tampoc no en veu el nom ni cap fragment.
- **No modifica mai la carpeta compartida.** Només la llegeix.

```
 Ordinador del treballador (VPN)                  Windows Server / Linux
┌────────────────────────┐   HTTP + token   ┌──────────────────────────────────────────┐
│ Claude Code            │ ───────────────► │ Servei "Hound"                            │
│  └─ MCP "hound"        │     /mcp         │  ├─ Servidor MCP (cerca + permisos)       │
└────────────────────────┘                  │  └─ Indexador (prioritat baixa)           │
                                            │        │ llegeix        │ escriu           │
                                            │   D:\Compartit     índex SQLite           │
                                            └──────────────────────────────────────────┘
```

## Què indexa

| Tipus | Extensions | Què se n'extreu |
|---|---|---|
| PDF | `.pdf` | Text per pàgina; **OCR** de les pàgines escanejades |
| Imatges | `.jpg .png .tif .bmp .webp` | Text per **OCR** (documents fotografiats, escanejos, rètols) |
| AutoCAD | `.dxf`, `.dwg` | Textos, atributs de blocs (caixetins), cotes i capes. Els DWG necessiten [ODA File Converter](#fitxers-dwg-dautocad); sense ell s'indexen pel nom |
| Word | `.docx .docm .odt .rtf` | Paràgrafs, taules, capçaleres |
| Excel | `.xlsx .xlsm .xls .ods .csv` | Files per full, amb la capçalera com a context |
| PowerPoint | `.pptx .pptm .odp` | Text i notes per diapositiva |
| Correu | `.msg .eml` | Capçalera, cos i **adjunts** (PDF, Word, Excel…) |
| Text | `.txt .md .log` | Paràgrafs |
| Altres | `.doc .ppt .zip .mp4`… | Només nom i carpeta |

**Tots** els fitxers també es poden trobar pel nom i la carpeta, fins i tot quan no se'n pot llegir el contingut.

Cada resultat indica **on** és la coincidència: "página 3", "hoja Gastos, filas 2–40", "diapositiva 5", "adjunto factura.pdf, página 1", "bloque CAJETIN"… De moment, les etiquetes d'ubicació són en castellà.

## Requisits

- **Windows:** Windows Server 2016 o posterior, de 64 bits.
- **Linux:** una distribució de 64 bits (x86_64 o aarch64) amb systemd: Debian/Ubuntu, RHEL/Rocky/Alma (amb EPEL), Fedora, openSUSE o Arch. La carpeta compartida sol servir-se amb Samba.
- Accés a internet durant la instal·lació.
- Uns 2 GB de disc per al programa i el model, més l'índex: uns 4–5 KB per fragment. Amb 1 milió de fragments són uns 5 GB.
- RAM per als vectors en memòria: uns 3 KB per fragment. Amb 1 milió són uns 3 GB, o la meitat amb `memory_dtype = "float16"`.
- No necessita GPU.

## Instal·lació

### Windows Server

Al servidor, obre **PowerShell com a administrador** i executa:

```powershell
irm https://raw.githubusercontent.com/abosch19/hound/main/install.ps1 | iex
```

Si el servidor només té un recurs compartit, el detecta sol; si en té diversos, pregunta quin. Per indicar els paràmetres, sobretot la **xarxa de la VPN** que s'hi podrà connectar:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/abosch19/hound/main/install.ps1))) -SharePath D:\Compartit -AllowFrom 10.8.0.0/24
```

Si prefereixes descarregar-lo primer amb `curl`:

```powershell
curl.exe -fsSL https://raw.githubusercontent.com/abosch19/hound/main/install.ps1 -o install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1 -SharePath D:\Compartit -AllowFrom 10.8.0.0/24
```

| Paràmetre | Per defecte | Què és |
|---|---|---|
| `-SharePath` | es detecta | Ruta local de la carpeta compartida |
| `-AllowFrom` | subxarxa local | Xarxa o xarxes que poden connectar-se al MCP, p. ex. la de la VPN. Separades per comes |
| `-Port` | `8765` | Port del MCP |
| `-ShareName` / `-Unc` | es detecten | Nom del recurs SMB i ruta `\\SERVIDOR\Recurs` que es mostra als resultats |
| `-OcrLanguages` | `spa,eng` | Idiomes d'OCR (p. ex. `spa,eng,cat`) |
| `-Version` | `main` | Branca o etiqueta de GitHub |
| `-NoOcr` | | No instal·la Tesseract |

L'instal·lador fa el següent:

1. Instal·la `uv` i un Python 3.12 propi a `C:\Program Files\Hound`, sense tocar el Python del sistema.
2. Instal·la Hound i les seves dependències.
3. Instal·la Tesseract amb els idiomes d'OCR.
4. Descarrega el model d'embeddings, una sola vegada.
5. Crea `C:\ProgramData\Hound\config.toml`.
6. Registra el servei **Hound**: arrencada automàtica i reinici si falla.
7. Activa a Windows el suport de rutes llargues (`LongPathsEnabled`).
8. Obre el port al tallafoc només per a `-AllowFrom`.

### Linux

Al servidor, com a root (o amb `sudo`):

```bash
curl -fsSL https://raw.githubusercontent.com/abosch19/hound/main/install.sh | sudo bash -s -- --share /srv/shared --allow-from 10.8.0.0/24
```

Sense `--share`, es llegeixen els recursos de Samba amb `testparm`: si n'hi ha un de sol s'utilitza, i si n'hi ha diversos pregunta quin.

| Paràmetre | Per defecte | Què és |
|---|---|---|
| `--share` | es detecta | Ruta local de la carpeta compartida |
| `--allow-from` | subxarxa local | Xarxa o xarxes que poden connectar-se al MCP, p. ex. la de la VPN. Separades per comes |
| `--port` | `8765` | Port del MCP |
| `--share-name` / `--unc` | es detecten | Nom del recurs de Samba i ruta `\\SERVIDOR\Recurs` que es mostra als resultats |
| `--ocr-languages` | `spa,eng` | Idiomes d'OCR (p. ex. `spa,eng,cat`) |
| `--version` | `main` | Branca o etiqueta de GitHub |
| `--no-ocr` / `--no-firewall` / `--no-service` | | No instal·la Tesseract, ni toca el tallafoc, ni crea el servei de systemd |

L'instal·lador fa el següent:

1. Instal·la `uv` i un Python 3.12 propi a `/opt/hound`, sense tocar el Python del sistema.
2. Instal·la Hound i les seves dependències.
3. Instal·la Tesseract i els idiomes d'OCR amb el gestor de paquets del sistema (apt, dnf/yum, zypper o pacman).
4. Descarrega el model d'embeddings, una sola vegada.
5. Crea `/etc/hound/config.toml` (amb `permissions.mode = "posix"`); l'índex, els tokens i els registres van a `/var/lib/hound`, llegible només per root.
6. Registra el servei **hound** de systemd: arrencada automàtica i reinici si falla. S'executa com a root per poder llegir tota la carpeta, però amb les capacitats reduïdes a la lectura de fitxers (`CAP_DAC_READ_SEARCH`) i la resta del sistema en només lectura.
7. Puja `fs.inotify.max_user_watches` si cal, perquè la vigilància de canvis pugui cobrir carpetes grans.
8. Obre el port només per a `--allow-from` amb **ufw** o **firewalld**, el que sigui actiu. Si no n'hi ha cap, t'avisa que has de restringir el port tu.
9. Instal·la l'ordre `hound` a `/usr/local/bin` (fes-la servir amb `sudo`).

### Seguir la indexació

En acabar la instal·lació, el servidor MCP ja respon i l'índex es construeix en segon pla. El servidor MCP és el primer que arrenca: l'indexador només comença quan el port ja és obert, de manera que pots seguir el progrés des del principi.

- Des del servidor: `hound status` (a Linux, `sudo hound status`).
- Des de Claude Code: pregunta *"com va la indexació de Hound?"*. L'eina `index_status` retorna la fase (escanejant, indexant, en pausa, al dia, aturat), el percentatge, els fitxers pendents i amb error, els fitxers per hora, el temps estimat que queda i quants fitxers pots veure. Només retorna recomptes, mai rutes.

> **Indexació inicial.** Amb molts fitxers (sobretot PDF escanejats i imatges amb OCR), la primera indexació pot durar hores o dies. En horari laboral l'indexador va a poc a poc, amb prioritat baixa de CPU i disc. Si prefereixes que només treballi de nit, posa `busy_mode = "pause"` a `config.toml`. Després només processa els canvis.

### Actualitzar

Torna a executar la mateixa ordre d'instal·lació (`install.ps1` o `install.sh`). Es conserven la configuració, l'índex i els tokens.

### Desinstal·lar

Al servidor, des d'una consola d'administrador (a Linux, amb `sudo`):

```bash
hound uninstall
```

Demana confirmació i conserva l'índex, els tokens i la configuració; afegeix `--purge` per esborrar-los també, i `-y` per no preguntar. A Windows continua en una finestra nova. També pots fer servir directament els scripts:

A Windows:

```powershell
irm https://raw.githubusercontent.com/abosch19/hound/main/uninstall.ps1 | iex
```

Conserva les dades a `C:\ProgramData\Hound`. Per esborrar-les també:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/abosch19/hound/main/uninstall.ps1))) -Purge
```

A Linux (es conserven les dades de `/etc/hound` i `/var/lib/hound`, tret que afegeixis `--purge`):

```bash
curl -fsSL https://raw.githubusercontent.com/abosch19/hound/main/uninstall.sh | sudo bash -s -- --purge
```

Tesseract no es desinstal·la en cap dels dos sistemes, perquè altres programes poden fer-lo servir.

## Afegir un usuari i connectar Claude Code

Cada treballador fa servir un **token personal** associat al **seu compte al servidor** (Windows, o Linux/Samba). Les cerques es fan amb els permisos d'aquest compte. Al servidor, com a administrador (a Linux, amb `sudo`):

```powershell
hound token create maria --host SERVIDOR
```

El compte pot ser `maria`, `EQUIP\maria` o `DOMINI\maria`: local si el servidor és en un grup de treball, o del domini si hi ha Active Directory. A Linux serveix qualsevol compte que el sistema conegui (`getent passwd maria`), també els del domini mitjançant winbind o sssd. L'ordre mostra el token **una sola vegada** i l'ordre exacta que la persona ha d'executar al seu ordinador, amb la VPN connectada:

```bash
claude mcp add --transport http --scope user hound http://SERVIDOR:8765/mcp --header "Authorization: Bearer hound_..."
```

Després, a Claude Code n'hi ha prou de demanar coses com *"busca a la carpeta compartida el contracte amb el proveïdor de neteja i digues-me quan venç"*.

Hound ofereix aquestes eines:

| Eina | Què fa |
|---|---|
| `search_files(query, folder?, file_type?, date_from?, date_to?, limit?)` | Cerca híbrida. Retorna ruta UNC, ubicació, fragment ressaltat, data i puntuació |
| `read_document(path, start?, end?)` | Text extret del fitxer per trams, sense carregar-lo sencer |
| `list_folder(path?)` | Subcarpetes i fitxers visibles per a l'usuari |
| `recent_files(limit?, folder?)` | Últims fitxers modificats |
| `index_status()` | Com va la indexació: fase, progrés, fitxers pendents, ritme i temps estimat que queda |

Amb `share.unc` configurat, els resultats inclouen també `file_url` i `folder_url` (`file://SERVIDOR/Recurs/...`), i Claude els mostra com a enllaços que es poden prémer: a Windows el primer obre el fitxer amb el seu programa i el segon obre l'Explorador a la seva carpeta. L'ordinador de l'usuari ha de poder accedir a la carpeta compartida (xarxa de l'oficina o VPN).

Altres ordres per gestionar els tokens:

```powershell
hound token list
hound token revoke maria   # tots els tokens de la maria
hound token revoke 7       # només el token amb id 7
```

Un token revocat deixa de funcionar en menys de 30 segons.

> **Per què Claude Code i no els connectors de claude.ai.** Els connectors remots de claude.ai es criden des dels servidors d'Anthropic, que no poden arribar a un servidor que només és accessible per VPN. Claude Code es connecta des de l'ordinador de l'usuari, que sí que és a la VPN. Hound no s'ha d'exposar a internet.

## Permisos

### Windows (NTFS)

- En indexar, Hound desa el descriptor de seguretat NTFS de cada fitxer: propietari, grup i ACL.
- En cercar, comprova amb l'API **Authz** de Windows si el compte del token pot llegir cada fitxer. Té en compte grups, grups niats, herència i permisos de "Denegar", igual que el mateix Windows.
- La comprovació es fa **abans** d'ordenar els resultats, de manera que un fitxer sense permís no apareix mai: ni a les cerques, ni a `read_document`, ni a `list_folder`, ni a `recent_files`.
- Si `share.name` està configurat, també s'apliquen els permisos del recurs compartit (pestanya "Compartir").
- Davant de qualsevol dubte (fitxer sense descriptor, error de Windows) l'accés es **denega**.
- Els canvis de permisos al disc s'apliquen al següent reescaneig: de seguida si els detecta la vigilància de canvis i, com a màxim, cada `rescan_minutes`.

Altres punts a tenir en compte:

- **Compte del servei.** El servei s'executa com a `SYSTEM`, que ha de poder llegir la carpeta compartida (és l'habitual). `hound doctor` ho comprova.
- **Servidor en domini.** Per calcular els grups d'un usuari del domini, el compte d'equip del servidor ha de poder llegir l'atribut `tokenGroupsGlobalAndUniversal`. Normalment ja pot; si `hound search --as DOMINI\usuari` falla, afegeix l'equip al grup **Windows Authorization Access Group** del domini.
- **Mode sense permisos.** Amb `permissions.mode = "none"` tots els usuaris amb token veuen tot l'índex. Fes-lo servir només si de debò tothom pot veure-ho tot.

### Linux (POSIX)

- En indexar, Hound desa el propietari, el grup, els bits de mode i l'ACL POSIX (`setfacl`) de cada fitxer, a més dels de cada carpeta des de l'arrel del recurs fins a ell.
- En cercar, aplica la mateixa comprovació que fa el nucli quan Samba obre un fitxer en nom de l'usuari: permís de lectura sobre el fitxer i de pas (`x`) a totes les carpetes del camí. Els grups es resolen amb NSS, de manera que funcionen els comptes locals, de winbind i de sssd, incloses les entrades d'ACL d'usuari i de grup amb nom i la màscara de l'ACL.
- Tota la resta funciona com a Windows: es comprova abans d'ordenar, es denega davant del dubte i els canvis es recullen al següent reescaneig.
- No ho cobreix: les regles exclusives de Samba a `smb.conf` (`valid users`, `read list`, `force user`…) ni les ACL de Windows desades per `vfs_acl_xattr` que no es reflecteixin a les ACL POSIX. Si en depens, fes servir exclusions o comprova-ho amb `hound search --as`.

Per comprovar què veuria un usuari concret:

```powershell
hound search "nòmines de març" --as maria
```

## Carpetes excloses

El que Claude llegeix amb aquestes eines s'envia a Anthropic per generar la resposta. Per això, les carpetes que no han de sortir del servidor (nòmines, RH, dades de salut…) s'exclouen de l'índex a `config.toml`:

```toml
[index]
exclude = ["RRHH", "Administracio/Nomines"]
```

El que s'exclou no s'indexa ni es pot consultar, i el que ja estigués indexat s'esborra de l'índex al següent escaneig. Les rutes són relatives a la carpeta compartida i no distingeixen majúscules.

## Fitxers DWG d'AutoCAD

DWG és un format tancat. Per llegir-ne el contingut (textos, caixetins, capes):

1. Descarrega i instal·la **ODA File Converter**, gratuït, des de https://www.opendesign.com/guestfiles/oda_file_converter. L'instal·lador no l'inclou perquè cal acceptar-ne la llicència manualment.
2. Reinicia el servei amb `Restart-Service Hound` (Linux: `sudo systemctl restart hound`). Hound el detecta a `C:\Program Files\ODA\` a Windows, o com a `ODAFileConverter` al `PATH` a Linux (el paquet `.deb`/`.rpm` l'instal·la a `/usr/bin`), i torna a processar automàticament els DWG que abans s'havien indexat només pel nom.

Si és en una altra ruta, indica-la a `[cad] oda_converter`.

## Operació

| Ordre | Per a què |
|---|---|
| `hound status` | Fase de l'indexador, progrés, fitxers pendents i amb error, ritme i temps estimat que queda (igual que l'eina `index_status`) |
| `hound errors` | Fitxers que no s'han pogut llegir i per què (PDF amb contrasenya, fitxers malmesos…) |
| `hound log --user maria` | Registre de consultes: qui ha cercat què i quan |
| `hound doctor` | Comprova carpeta, model, Tesseract, ODA i permisos |
| `hound reindex --errors` | Torna a provar els fitxers amb error |
| `hound reindex --path "Obres\2024"` | Torna a indexar una carpeta o un fitxer |
| `hound reindex --all` | Refà l'índex sencer (p. ex. després de canviar de model). Conserva els tokens i el registre |
| `hound backup D:\Copies` | Còpia de seguretat en calent de l'índex |
| `Restart-Service Hound` / `sudo systemctl restart hound` | Aplica els canvis de `config.toml` |

Els registres són a `C:\ProgramData\Hound\logs` a Windows, i a `/var/lib/hound/logs` i `journalctl -u hound` a Linux. A Linux, executa aquestes ordres amb `sudo`.

### Còpia de seguretat i restauració

L'índex (`C:\ProgramData\Hound\index.sqlite`, o `/var/lib/hound/index.sqlite` a Linux) inclou els usuaris, els tokens i el registre de consultes. Per fer-ne la còpia sense aturar el servei:

```powershell
hound backup D:\Copies
```

La pots programar amb el Programador de tasques (a Linux, `sudo hound backup /backups` des de cron o un temporitzador de systemd). Per restaurar-la:

1. `Stop-Service Hound` (Linux: `sudo systemctl stop hound`).
2. Copia el fitxer damunt d'`index.sqlite` i esborra `index.sqlite-wal` i `index.sqlite-shm` si existeixen.
3. `Start-Service Hound` (Linux: `sudo systemctl start hound`).

Si es perd l'índex, no es perd res de la carpeta compartida: n'hi ha prou amb `hound reindex --all` i tornar a crear els tokens.

## Configuració

El fitxer és `C:\ProgramData\Hound\config.toml` a Windows i `/etc/hound/config.toml` a Linux. Després de canviar-lo, executa `Restart-Service Hound` o `sudo systemctl restart hound`.

| Secció | Opció | Per defecte | Descripció |
|---|---|---|---|
| `share` | `path` | | Ruta local de la carpeta compartida |
| | `unc` | | Ruta UNC que es mostra als resultats |
| | `name` | | Recurs SMB els permisos del qual també s'apliquen |
| `server` | `host`, `port` | `0.0.0.0`, `8765` | On escolta el MCP |
| | `tls_cert`, `tls_key` | | Rutes PEM per servir HTTPS |
| `index` | `exclude` | `[]` | Carpetes excloses |
| | `exclude_names` | paperera, `~$*`, `Thumbs.db`… | Noms ignorats (glob) |
| | `extensions` | vegeu més amunt | Extensions que s'indexen |
| | `max_file_mb` | `300` | Per sobre, només pel nom |
| | `rescan_minutes` | `360` | Reescaneig complet periòdic |
| | `watch` | `true` | Vigilància de canvis en temps real |
| | `extract_workers` | `2` | Processos d'extracció en paral·lel |
| `index.schedule` | `busy_hours`, `busy_days` | `08:00-19:00`, `mon-fri` | Horari laboral |
| | `busy_mode` | `slow` | `slow`, `pause` o `normal` |
| `ocr` | `enabled`, `languages`, `dpi` | `true`, `spa+eng`, `300` | OCR amb Tesseract |
| `cad` | `oda_converter` | es detecta | Ruta a `ODAFileConverter.exe` |
| `model` | `repo`, `file`, `dim` | EmbeddingGemma-300M int8, 768 | Model d'embeddings ONNX |
| | `threads` | la meitat dels nuclis | Fils de CPU per als embeddings |
| `search` | `memory_dtype` | `float32` | `float16` fa servir la meitat de RAM i és una mica més lent |
| `permissions` | `mode` | `ntfs` | `ntfs` o `none` |

Sobre HTTPS: dins de la VPN el trànsit ja va xifrat. Si tot i així vols fer servir HTTPS amb un certificat propi, els usuaris han de configurar `NODE_EXTRA_CA_CERTS` amb la CA perquè Claude Code l'accepti.

## Com funciona

- **Índex:** un únic SQLite en mode WAL, a l'estil de [DigUp](https://github.com/ARahim3/DigUp). L'indexador és l'únic que hi escriu; el servidor només llegeix. Taules:
  - `files`: ruta, tipus, mida, data, estat i descriptor de seguretat;
  - `segments`: fragments amb la ubicació, el text i el vector en float16;
  - `segments_fts`: índex de paraules clau FTS5, sense accents;
  - `security_descriptors`: deduplicats, perquè gairebé tots els fitxers hereten els permisos de la seva carpeta;
  - `users`, `tokens` i `query_log`.
- **Model:** [EmbeddingGemma-300M](https://huggingface.co/onnx-community/embeddinggemma-300m-ONNX), quantitzat int8, amb ONNX Runtime a la CPU. És multilingüe, amb bona qualitat en castellà, i la meitat de pesant que bge-m3. Es descarrega una vegada i després funciona sense connexió.
- **Cerca híbrida:**
  1. Similitud cosinus contra tots els vectors en memòria (numpy, per blocs).
  2. En paral·lel, FTS5 amb BM25 per a les paraules clau.
  3. Les dues llistes es combinen amb *Reciprocal Rank Fusion* i s'agrupen per fitxer.

  Amb 1 milió de fragments, la part vectorial triga uns 120 ms.
- **Indexació incremental:**
  - un reescaneig periòdic compara la mida, la data i els permisos de cada fitxer;
  - la vigilància de canvis (`ReadDirectoryChangesW` a Windows, inotify a Linux) reacciona a l'instant;
  - l'extracció s'executa en processos a part amb un temps màxim per fitxer, i un fitxer que falla es registra sense aturar res.

## Limitacions conegudes

- Els `.doc` i `.ppt` antics (format binari) s'indexen només pel nom.
- Les imatges es cerquen pel text que contenen (OCR), no pel que mostren. La cerca de fotos pel contingut i l'àudio queden per a una fase posterior.
- Les etiquetes d'ubicació i els missatges del servidor són en castellà.
- Les rutes es comparen sense distingir majúscules, com a Windows. A Linux, dos fitxers de la mateixa carpeta els noms dels quals només es diferencien en les majúscules s'indexen com un de sol.

## Desenvolupament

```bash
uv venv && uv pip install -e ".[dev]"
python -m pytest
```

- Els tests de permisos amb ACL reals (`tests/test_windows_acl.py`) creen usuaris locals: només s'executen a Windows amb `HOUND_WIN_ACL_TESTS=1`. La CI de GitHub ho fa a `windows-latest`, juntament amb una instal·lació completa amb `install.ps1`.
- Els tests de permisos POSIX (`tests/test_posix_acl.py`) s'executen a Linux i macOS; el d'ACL reals necessita `setfacl`. La CI també fa una instal·lació completa a `ubuntu-latest` amb `install.sh`, comprova que una carpeta sense permís continua amagada i desinstal·la.
- `HOUND_BENCH=1` activa la prova de rendiment amb 1 milió de fragments.

L'encàrrec original (en castellà) és a [docs/PROMPT.md](docs/PROMPT.md).

## Llicència

MIT
