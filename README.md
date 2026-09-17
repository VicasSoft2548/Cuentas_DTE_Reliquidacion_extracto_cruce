# Cuentas por cobrar — web local

Conversión del libro de Excel a una aplicación web local con base SQLite.

## Qué hace
- Mantiene las pestañas funcionales equivalentes a: Dashboard, clientes/deuda, no emparejados, cruce, DTE, extracto, Empresa y subir_datos.
- **DTE y Reliquidación siempre forman parte del libro de deuda.** Las reliquidaciones negativas se conservan como ajustes firmados y afectan el saldo neto.
- La pestaña **No emparejados** sirve únicamente para el emparejamiento manual.
- Un emparejamiento guardado manualmente se almacena en SQLite con `source = MANUAL`; deja de aparecer entre pendientes y se conserva al volver a importar el mismo registro.
- Importa `.xlsx` y `.xlsm` sin ejecutar macros. El lector usa la estructura XML interna de Excel.
- Incluye cruces importados del Excel, cruces automáticos conservadores (monto exacto y único) y cruces manuales.
- Dashboard con KPIs y gráficos: cartera por empresa, antigüedad, estado de cartera y cobros por mes.

## Lenguajes / tecnología
- Backend: **Python (servidor HTTP integrado, sin librerías externas)**
- Base de datos: **SQLite**
- Interfaz: **HTML + CSS + JavaScript**
- Gráficos: Canvas JavaScript propio (funciona sin CDN)

## Ejecutar en Windows
1. Instala Python 3.11 o superior desde python.org si todavía no lo tienes.
2. Descomprime la carpeta.
3. Haz doble clic en `iniciar_windows.bat`.
4. Se abrirá `http://127.0.0.1:5000`.

No necesita instalar Flask ni ninguna otra librería de Python.

## Ejecutar manualmente
```bash
python app.py
```

## Importar una nueva versión del Excel
Usa la pestaña **Subir datos** y selecciona tu `.xlsm` o `.xlsx`.

Se esperan estas hojas cuando existan:
- `DTE`: A Fecha, B Concepto, C Nombre empresa, D Monto
- `extracto`: A Fecha/hora, B Sucursal/agencia, C Descripción, D Cheque/referencia, E Código transacción, F Créditos
- `Empresa`: B Empresa, C Abreviatura
- `clientes`: se usa para migrar los emparejamientos que Excel ya daba por resueltos.

Los cruces manuales se mantienen porque cada deuda y depósito recibe una huella estable basada en sus datos de origen.

## Regla de emparejamiento automático
La web solo empareja automáticamente cuando queda una única deuda positiva y un único depósito con exactamente el mismo monto y el depósito no es anterior al DTE. Los casos dudosos quedan en **No emparejados** para decisión manual.

## Base de datos
El archivo está en `data/cuentas.db`. Para hacer copia de seguridad, basta copiar ese archivo con la aplicación cerrada.

## Cambios de la versión 1.1

- Pestaña **Reliquidación** visible y separada de DTE, manteniéndose ambas dentro del cálculo de deuda.
- Búsqueda por monto en **Deuda**.
- Búsqueda por monto en ambos lados de **No emparejados**, con tolerancia configurable.
- Botones para buscar rápidamente la deuda por el monto del depósito seleccionado y viceversa.


### Emparejamiento manual múltiple
Un mismo depósito puede distribuirse entre varios DTE/Reliquidaciones. La pantalla también busca combinaciones de 2 documentos cuya suma coincida con el depósito dentro de la tolerancia configurada. Cada aplicación queda almacenada como MANUAL en SQLite.


## Reversos bancarios
La versión V3 separa Debe/Reverso y Haber/Crédito en Extracto. Detecta un negativo que anula un positivo previo por el mismo monto y fecha. Si luego aparece el mismo positivo de nuevo, lo marca como REAPLICADO y solo ese movimiento final queda disponible para emparejar.
