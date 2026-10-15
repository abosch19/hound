Quiero que construyas **Hound**, un buscador semántico interno para los ficheros compartidos de mi empresa. Antes de escribir código, hazme las preguntas de la sección "Preguntas iniciales" y propón un plan; no empieces a implementar hasta que lo apruebe.

## Contexto
- Somos 6-7 trabajadores. Tenemos un servidor Windows (on-premise) con una carpeta compartida por SMB que cada uno monta como unidad de red en Windows, conectándonos por VPN.
- Queremos buscar en esos ficheros por significado (embeddings), no solo por nombre: "el contrato con el proveedor de limpieza", "la factura de marzo de X", "el presupuesto de la reforma".
- Referencia de inspiración: DigUp (https://github.com/ARahim3/DigUp), una app de escritorio que hace esto en local. Su índice es un único SQLite con: tabla `files` (path, kind, size, mtime, estado de indexado), tabla `segments` (fragmentos de cada fichero: página de PDF, párrafo, tramo de audio; con excerpt de texto y `vec BLOB` de 768 float16 normalizados) y una tabla FTS5 para palabras clave. Busca cargando los vectores en memoria y comparándolos por fuerza bruta, combinado con FTS5 (búsqueda híbrida). Queremos el mismo enfoque, pero como servicio en el servidor para varios usuarios.

## Arquitectura deseada
Una sola aplicación en Python que corre en el propio servidor Windows como servicio (que arranque sola y se reinicie si falla), con tres partes que comparten el mismo índice:

1. **Indexador**
   - Recorre la carpeta compartida (acceso local, no por red) en modo SOLO LECTURA: nunca modifica, mueve ni borra ficheros del disco compartido.
   - Indexado incremental: detecta ficheros nuevos, modificados (mtime/tamaño) y borrados; reescaneo periódico más vigilancia de cambios si es fiable en Windows.
   - Prioridad baja de CPU/IO para no molestar durante la jornada; la indexación inicial grande puede programarse fuera de horario.
   - Extrae texto de: PDF (incluido OCR para escaneados, con Tesseract u otro motor local), Word, Excel, PowerPoint, correos .msg/.eml, texto plano y CSV. Imágenes y audio/vídeo, en una fase posterior y opcional.
   - Divide en fragmentos con su ubicación (página, hoja, párrafo) para poder citar dónde está la coincidencia.
   - Lista configurable de carpetas y extensiones excluidas.
   - Registra los ficheros que fallan y por qué, sin detenerse.

2. **Índice y búsqueda**
   - SQLite en modo WAL (un escritor: el indexador; varios lectores: web y MCP), con tablas files, segments y FTS5 al estilo DigUp, más una tabla de permisos.
   - Embeddings con un modelo local y multilingüe (buena calidad en español), ejecutable en CPU: propón entre EmbeddingGemma y bge-m3 u otro, justificándolo. Nada de APIs externas: los documentos no pueden salir del servidor durante la indexación.
   - Búsqueda híbrida: similitud coseno sobre los vectores en memoria (numpy) + FTS5, combinadas con Reciprocal Rank Fusion. Filtros por carpeta, tipo de fichero y rango de fechas.
   - Objetivo de escala: hasta ~1 millón de fragmentos con respuesta en menos de 1 s.

3. **Interfaces**
   - **Web** accesible por la VPN: caja de búsqueda, resultados con nombre, ruta, fragmento resaltado, página/ubicación y fecha; botón para copiar la ruta UNC (\\servidor\carpeta\...) para abrirla desde el Explorador.
   - **Servidor MCP** estándar (transporte Streamable HTTP), pensado para cualquier cliente MCP: Claude Code en el equipo de cada trabajador, y también agentes automáticos (Claude Agent SDK, n8n u otros) que corran en el propio servidor o en otra máquina de la VPN.
     Herramientas:
     - `search_files(query, folder?, file_type?, date_from?, date_to?, limit?)`: devuelve ruta, fragmento, ubicación, fecha y puntuación.
     - `read_document(path, start?, end?)`: devuelve el texto extraído por tramos, sin cargar documentos enteros de golpe.
     - `list_folder(path)` y `recent_files(limit?, since?)`.
     Requisitos para que sirva a agentes, no solo a personas:
     - Esquemas de entrada y salida estables, documentados y con resultados en JSON estructurado (campos fijos: path, unc_path, excerpt, location, modified, score), además del texto legible.
     - Descripciones de las herramientas claras para un modelo: qué hace cada una, cuándo usarla y límites (máximo de resultados y de caracteres por lectura).
     - Paginación o cursores en búsquedas y lecturas largas.
     - Errores claros y accionables (fichero no encontrado, sin permiso, documento aún no indexado) en vez de excepciones genéricas.
     - Límite de peticiones por cliente y tiempo máximo por llamada, para que un agente en bucle no sature el servidor.
     - Versionado: si cambian las herramientas, que los clientes antiguos sigan funcionando o reciban un error claro.
     - El MCP es solo de lectura: ninguna herramienta modifica, mueve ni borra ficheros.
   - Ten en cuenta que los conectores remotos de claude.ai se llaman desde los servidores de Anthropic y no pueden alcanzar un servidor que solo es accesible por VPN. Los clientes válidos son los que se ejecutan dentro de la red: Claude Code en el equipo del usuario (`claude mcp add --transport http ...`) o agentes alojados en el propio servidor o en la VPN. No expongas nada a internet.

## Seguridad y permisos (requisito crítico)
- Cada búsqueda, en la web y en el MCP, se hace COMO el usuario que pregunta, nunca con una cuenta de servicio que lo ve todo.
- Si hay carpetas con permisos restringidos: lee las ACL de NTFS de cada fichero al indexar, guárdalas en el índice y filtra los resultados según el usuario y sus grupos. Un usuario nunca debe ver ni el nombre ni fragmentos de un fichero al que no tiene acceso en el disco.
- Autenticación: si el servidor está en un dominio de Active Directory, usa las credenciales de Windows/AD; si no, una lista sencilla de usuarios gestionada en la configuración. Para el MCP, token personal por usuario, revocable.
- Identidades para agentes: además de los usuarios, permite crear "cuentas de agente" con su propio token, revocable, y con un alcance explícito (qué carpetas puede ver), sin heredar los permisos de nadie por defecto. Un agente nunca debe tener acceso a todo salvo que se configure así a propósito.
- Si un agente actúa en nombre de un usuario concreto, que pueda pasar la identidad de ese usuario y los resultados se filtren con sus permisos.
- Lista configurable de carpetas excluidas del MCP (por ejemplo nóminas o RR. HH.), porque lo que Claude o un agente con un modelo en la nube lee se envía fuera del servidor para generar la respuesta. Se aplica igual a personas y a cuentas de agente.
- Registro de consultas (quién buscó qué y cuándo), distinguiendo si fue una persona o un agente, y cuál.

## Entregables
- Código con tests (extracción, troceado, ranking híbrido y, sobre todo, filtrado por permisos con casos negativos).
- Un fichero de configuración (ruta de la carpeta compartida, exclusiones, puerto, modelo, usuarios, cuentas de agente).
- Script de instalación en Windows Server que instale dependencias, descargue el modelo una sola vez y registre el servicio.
- README en español: instalación, cómo añadir un usuario o una cuenta de agente, cómo conectar Claude Code y otros agentes al MCP, copia de seguridad del índice y cómo reindexar.
- Por fases: (1) indexador + búsqueda + web, (2) permisos y autenticación, (3) MCP para personas y agentes, (4) opcionales: OCR de imágenes, audio, búsqueda de fotos.

## Preguntas iniciales (pregúntamelas antes de empezar)
1. ¿El servidor está en un dominio de Active Directory o es un grupo de trabajo?
2. ¿Hay carpetas con permisos restringidos o todo el mundo lo ve todo?
3. Versión de Windows Server, CPU, RAM, disco libre y si tiene GPU.
4. Ruta local de la carpeta compartida, volumen aproximado (GB y número de ficheros) y tipos de fichero predominantes.
5. ¿Hay muchos PDFs escaneados que necesiten OCR?
6. ¿Se puede instalar Python en el servidor? ¿Qué puerto podemos usar dentro de la VPN?
7. ¿Hay carpetas que no deban indexarse o que no deban ser accesibles desde Claude?
8. ¿Qué agente o agentes se conectarán (Claude Agent SDK, n8n, otro), dónde se ejecutarán (en el servidor, en la VPN o en la nube) y qué tareas harán?
