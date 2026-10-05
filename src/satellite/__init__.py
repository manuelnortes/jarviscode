"""Satélite de voz ambiente de Jarvis (Hito 3.5+4).

Paquete **autónomo** que corre en su propio contenedor (`jarvis-satellite`) y
habla el protocolo `/voice` del núcleo como si fuera otro navegador. NO importa
nada de ``src.core`` / ``src.voice`` a propósito: su imagen es mínima (captura de
audio + wake word + cliente WS), independiente de la imagen pesada del núcleo.

Fase 2 (este commit): captura de la PS Eye + cliente WS en modo ``test-once``
(graba unos segundos y los manda como una utterance). El wake word y la máquina
de estados completa llegan en la Fase 3.
"""
