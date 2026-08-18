"""Erkennungs-Engine des PTI-Pseudonymisierers.

Die Engine setzt auf Presidios ``AnalyzerEngine`` auf und ergänzt sie um ein
deutsches spaCy-Modell sowie um schweizspezifische Erkenner.

Aufbau des Pakets
-----------------
``entities``
    Katalog aller Entitätstypen mit deutschem Label und Default-Policy.
``nlp``
    Die spaCy-NLP-Engine als Modul-Singleton (Ladezeit ~7 s, ~400 MB).
``recognizers``
    Die eigenen Erkenner: AHV, CH/LI-IBAN, Telefon, PLZ, Kontrollschild und
    die gepflegte Begriffsliste.
``merge``
    Auflösung überlappender Treffer.
``analyzer``
    Zusammenbau von allem und die Funktion :func:`analyze`.

Bewusst **keine** Importe der schweren Module auf Paketebene: ein
``import backend.engine`` darf nicht 400 MB spaCy-Modell nach sich ziehen.
Wer die Engine braucht, importiert ``backend.engine.analyzer`` gezielt.
"""

__all__ = ["analyzer", "entities", "merge", "nlp", "recognizers"]
