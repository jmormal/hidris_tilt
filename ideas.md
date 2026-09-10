# Ideas

Estos escenarios no se estan guarando todavía. Es fácil crear una base de datos y integrarlo en la api para que cada usuario pueda realizar los experimentos que considere. Pero hay varias cosas ha tener en cuenta.

## Integración de la plataforma con datos en tiempo real

- Para integrar la plataforma con datos observados y las predicciones hay dos posibilidades. La primera es una simulación de toda la cuenca a partir, donde las condiciones iniciales del modelo la tormenta en tiempo real.  La segunda toma los datos de entrada proporcionados por un modelo hidrológico y que entran al sistema como información de caudal y no como lluvia.

### Tormenta en tiempo real + predicción en tiempo real

En este caso hay que hacer un paso previo que aun no esta implementado. Este es transformar los datos en tiempo real en una estimación de la tormenta que esta ocurriendo. Esto seria un servicio aparte y donde hay mucho que investigar y desarrollar, ya que se pueden mezclar diferentes fuentes de datos y montar un sistema de ia o un método clásico que de una estimación de la tormenta en tiempo real. En Tetis esto esta implementado dentro del propio modelo, que ha sido calibrado previamente a partir de datos históricos. La estimación proporciando por Tetis no tiene en cuenta fuentes de informacion externas a los puntos de medición establecidos en el barranco del poyo. Además, tampoco tiene en cuenta nuevos sistemas de información como puede ser los datos proporcionados por el radar o las previsiones meteorológicas.

### Basado en cuadal estimado de entrada en tiempo en real

Este es el sistema inicialmente planteado. Donde se estima el caudal de entrada en la zona de l'horta sud a partir del modelo hidrológico Tetis. El modelo hidrológico Tetis esta calibrado solo para el Barranco del Poyo. Por tanto, no es extrapolable sin una calibaración previa a otras zonas.

## Simulación de tormentas históricas

Proporcionar un servicio de estimación de tormentas históricas que puedan ser utilizadas para realizar experimentos. También se puede realizar de forma automática con las tormentas observadas.

## Plataforma de computación

Anuga (el simulador hidráulico utilizado) es altamente paralelizable. Se puede utilizar tanto en CPUs como en GPUs. Ahora mismo hay dos formas de resolverlo. En local donde la plataforma esta instalada. O en el servidor HPC.

### Resolución local

Se gastan los recursos que esten instalados en el cluster de Kubernetes. Ahora mismo, al estar desplegado solo en mi ordenador, solo se puede utilizar GPU 5060 ti Mobile.

### Resolución HPC

También se puede resolver en HPC vrhpcadm1.disc.upv.es. Solo que es de prueba ya que es algo bastante cutre. Para poder encolar el trabajo me tengo que conectar por ssh con mi cuenta y usuario al servidor.

## Analisis de Coste Social y Économico por simulaciones

## Integración de nuevos territorios (Api MDT)

## Visualización 3D del evento

## Validación de la herramienra IIAMA

## Optimización de compresión y lectura en función de los poligonos.(Igual se puede hacer mediante un tiler pero hay que investigar exactamente como funciona)

## Tests de stress de los diferentes sistemas (frontend, computo (paralización, ...))
