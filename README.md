# Hound

Buscador semántico interno para los ficheros compartidos de la empresa.

Hound indexa la carpeta compartida del servidor Windows y permite buscar documentos por su significado ("el contrato con el proveedor de limpieza", "la factura de marzo de X"), no solo por su nombre. Se usa desde:

- **una web** accesible por la VPN;
- **un servidor MCP** para Claude Code y para agentes automáticos.

Todo funciona dentro del servidor: los documentos no salen de él durante la indexación, y cada búsqueda respeta los permisos de quien la hace.

## Estado

En diseño. El encargo completo, con la arquitectura, los requisitos y las preguntas pendientes, está en [docs/PROMPT.md](docs/PROMPT.md).
