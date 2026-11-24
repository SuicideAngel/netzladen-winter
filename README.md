# Netzladen Winter

<img src="custom_components/netzladen_winter/brand/icon.png" alt="Netzladen Winter" width="72" height="72" align="right">

Home-Assistant-Integration für die Netzladung eines Heimspeichers. Sie berechnet den Ziel-Ladestand aus Verbrauch, PV-Prognose und optionalem Wetter. Alle Einstellungen liegen in der Home-Assistant-Oberfläche.

## Installation

**Voraussetzung:** Home Assistant 2026.11 oder neuer.

1. In HACS unter **⋮ → Benutzerdefinierte Repositories** dieses Repository als **Integration** hinzufügen. [GitHub-Repository](https://github.com/SuicideAngel/netzladen-winter)
2. **Netzladen Winter** herunterladen und Home Assistant neu starten.
3. Unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** nach **Netzladen Winter** suchen. Änderungen sind später unter **Konfigurieren** möglich.

Manuell kannst du den Ordner `custom_components/netzladen_winter` in den gleichnamigen Pfad deiner Home-Assistant-Konfiguration kopieren.

## Einrichtung

| Bereich | Was du einstellst |
| --- | --- |
| **Speicher** | Aktueller, minimaler und maximaler Ladestand, Kapazität und Backupreserve. Die Ladegrenzen für Speicher und Netz sind separat einstellbar. Beim Netzladen gilt der kleinere Wert. |
| **Verbrauch** | Fortlaufender Hauszähler oder Energiebilanz aus PV-, Netz- und Batteriezählern. Bei Bedarf Zähler für Wärmepumpe und Wallbox auswählen. Die Wallbox wird vom gemessenen Hausverbrauch abgezogen. |
| **Prognose** | Solcast-Sensor mit `detailedHourly`, optional zweiter Prognosesensor, Wetter und Außentemperatur. Bis genug Messwerte vorliegen, gilt die eingestellte Ersatzlast. |
| **Tarif** | Festes Fenster ab Mitternacht bis zum einstellbaren Ladeende oder dynamische Freigabe über einen aktuellen Preissensor. Beide Modi gelten in den ausgewählten Monaten. |

Im **dynamischen Modus** kann die Ladung unter der eingestellten Preisgrenze starten. Bei genau der Grenze läuft eine begonnene Ladung weiter. Oberhalb der Grenze oder bei einem ungültigen Preis endet die Freigabe. Der Sensor darf Preise in ct/kWh, EUR/kWh oder EUR/MWh liefern.

## Vorschau und Steuerung

Die **Vorschau** ist voreingestellt. Die Integration zeigt Ziel-Ladestand, konservative PV-Prognose und Betriebsstatus an.

Für die **aktive Steuerung** wählst du drei Regler für Ladegrenze, Zwangsladeleistung und maximalen SoC, zwei Auswahl-Entitäten für EMS-Modus und Ladebefehl sowie deren tatsächliche Rückmeldungen. Die Leistungsregler können W oder kW verwenden. Ohne gültige Speicherdaten und stündliche PV-Prognose startet keine Netzladung.

Nach dem Laden fordert die Integration Stopp und Eigenverbrauch an und setzt die Regler zurück. Die wiederhergestellte Ladegrenze bleibt auf das eingestellte Speichermaximum begrenzt. Eine ausstehende Freigabe erscheint im Betriebsstatus und als Benachrichtigung. Bei einem Home-Assistant-Ausfall ist die Freigabe erst nach dem Wiederanlauf möglich.

Wenn das alte Sungrow-Paket läuft, schalte vor der Aktivierung `input_boolean.sg_winter_aktiviert` aus und prüfe, dass `input_boolean.sg_winter_uebernommen` aus ist. Eine konkurrierende Automation kannst du als Sperre auswählen. Wechsle vor dem Entfernen der Integration zur Vorschau und prüfe die Freigabe am Wechselrichter.
