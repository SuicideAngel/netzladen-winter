# Netzladen Winter

Netzladen Winter berechnet in Home Assistant ein Ladeziel für einen Heimspeicher. Der Plan berücksichtigt aus Energiezählern gelernte Haushaltslast, die stündliche PV-Prognose und bei passender Datenlage Wetter und Wärmepumpe. Die Einstellungen liegen vollständig in der Home-Assistant-Oberfläche.

## Installation

Voraussetzung ist Home Assistant 2026.11 oder neuer. Für HACS steht das [GitHub-Repository Netzladen Winter](https://github.com/SuicideAngel/netzladen-winter) zur Verfügung.

1. In HACS das Menü **⋮ → Benutzerdefinierte Repositories** öffnen, die URL des GitHub-Repositories eintragen und als Typ **Integration** wählen.
2. **Netzladen Winter** herunterladen und Home Assistant neu starten.
3. **Einstellungen → Geräte & Dienste → Integration hinzufügen → Netzladen Winter** öffnen. Dieselben Einstellungen lassen sich anschließend unter **Konfigurieren** ändern.

Für eine manuelle Installation den Ordner `custom_components/netzladen_winter` in das gleichnamige Verzeichnis deiner HA-Konfiguration kopieren. Danach ebenfalls neu starten und die Integration hinzufügen.

## Entitäten und Einstellungen

| Bereich | Auswahl in Home Assistant |
| --- | --- |
| Speicher | Ladestand, Mindest- und Maximal-SoC. Speicherkapazität und Backupreserve als Zahlenwert oder mit optionalem Sensor. Der Schalter für skalierten Ladestand legt fest, ob die Batterie bereits zwischen eingestelltem Mindest- und Maximal-SoC auf 0–100 % umrechnet. Das ist bei der bisherigen Sungrow-Konfiguration der Fall. |
| Hausverbrauch | Einen fortlaufenden Hausverbrauchszähler verwenden oder aus PV-Erzeugung, Netzbezug, Netzeinspeisung und Batterieladung und -entladung bilanzieren. Ein zweiter PV-Erzeugungszähler ist bei der Bilanz optional und muss eine zusätzliche Anlage messen. |
| Wärmepumpe | Bei aktivierter Option den fortlaufenden Stromzähler der Wärmepumpe auswählen. Ihr Verbrauch bleibt Teil des Hausverbrauchs. Gelernte Messpaare aus Verbrauch und Außentemperatur erlauben eine Wetterkorrektur. |
| Wallbox | Bei aktivierter Option den fortlaufenden Ladeenergiezähler auswählen. Die Integration zieht den Wallboxverbrauch vom gemessenen Hausverbrauch ab. Die gewählte Hausmessung oder Bilanz muss ihn daher enthalten. |
| Planung | Solcast-PV-Sensor für heute und optional morgen, Wetterentität mit Stundenprognose und optionaler Außentemperatursensor. Bei Solcast die Attribute `detailedHourly` für die gewählten Sensoren bereitstellen. Beide Prognosen werden nach dem Datum ihrer Stundenwerte geprüft. |
| Parameter | Ladeleistung in W, PV-Schwelle für Vollladung, Puffer in kWh, stündlicher Startwert in kW und aktive Monate. |
| Stromtarif | Festes Zeitfenster mit einstellbarem Ladeende oder dynamische Freigabe über einen Preissensor mit Preisgrenze in ct/kWh. |

Energiezähler sollen fortlaufende Gesamtwerte in Wh, kWh oder MWh liefern. Bei der Bilanz sind Netzbezug, Einspeisung, Batterieladung und -entladung jeweils positive Zähler. Das Lernprofil verwendet bis zu 14 abgeschlossene Tage. Eine Stunde nutzt den einstellbaren Startwert, bis mindestens drei passende Messungen vorliegen. Bei neuen Sensorzuordnungen für das Lernprofil beginnt die Messung mit den neuen Quellen. Die Integration übernimmt keine alten Messreihen aus dem YAML-Paket.

Die Integration legt Sensoren für Ziel-Ladestand, konservative PV-Prognose und Betriebsstatus an. Der Zielsensor enthält unter anderem Begründung, Ladeplan, Stundenprofil und Verbrauchsvergleich. Die PV-Entität zeigt in ihren Attributen den Einfluss von Nebel und fehlendem Wetter.

## Stromtarif

Unter **Konfigurieren → Stromtarif** stehen zwei Modi zur Auswahl. Beide gelten in den gewählten aktiven Monaten.

- **Festes Zeitfenster:** Netzladen kann ab Mitternacht bis zum eingestellten Ladeende stattfinden. Für bestehende Installationen gilt weiterhin das konfigurierte Ladeende. Der Standard ist 05:00 Uhr.
- **Dynamischer Preissensor:** Wähle einen Sensor mit dem aktuellen Arbeitspreis und gib die Obergrenze in ct/kWh ein. Bei **20 ct/kWh** kann die Netzladung unter **0,20 €/kWh** starten. Bei genau **0,20 €/kWh** bleibt eine laufende Ladung freigegeben, solange das Ladeziel noch nicht erreicht ist. Die Integration unterstützt `ct/kWh`, `¢/kWh`, `EUR/kWh`, `€/kWh`, `EUR/MWh` und `€/MWh`. Wähle für eine Preisgrenze auf Basis deines Endkundenpreises einen Sensor, der die gewünschten Steuern und Aufschläge bereits enthält.

Im Preismodus prüft die Integration jede Preisänderung und zusätzlich einmal pro Minute. Sie nutzt den aktuellen Preis für die Freigabe und berechnet das Ladeziel aus der verbleibenden Haushaltslast und PV-Prognose des Tages. Bei einem Preis über der Grenze, einem fehlenden Preis oder einer unbekannten Einheit endet die Freigabe. Der Zielsensor zeigt den umgerechneten Preis, die Preisgrenze und den Freigabestatus in seinen Attributen.

## Speichersteuerung

Die Vorschau ist voreingestellt. Für aktive Steuerung stehen drei schreibbare Zahlen-Entitäten für Ladebegrenzung, Zwangsladeleistung und maximalen SoC sowie zwei Auswahl-Entitäten für EMS-Modus und Ladebefehl zur Verfügung. Zwei zusätzliche Entitäten melden die tatsächlichen Betriebszustände des Wechselrichters zurück. Die voreingestellten Texte entsprechen dem vorhandenen Sungrow-Paket. Zahlen-Entitäten für die Leistung können in W oder kW arbeiten. Einheitenlose Leistungs-Helper werden als W behandelt.

Die Übernahme beginnt im bestätigten Eigenverbrauch mit gestopptem Ladebefehl. Bei Tariffreigabe darf der Regler die Ladegrenze auf den eingestellten Wert und den Maximal-SoC vorübergehend auf 100 % setzen. Der Mindest-SoC dient als Messwert. Beim Freigeben fordert die Integration Stopp und Eigenverbrauch an und stellt nach deren Bestätigung die vorherigen Grenzen wieder her. Der Übernahmezustand wird lokal gespeichert. Eine ausstehende Freigabe wird im Betriebsstatus und in einer HA-Benachrichtigung angezeigt. Bei ungültigen Speicherdaten oder fehlender stündlicher PV-Prognose startet keine Netzladung. Bei fehlenden Wetterdaten gelten konservativere PV-Annahmen.

Wenn das alte Paket schon läuft, vor Aktivierung der neuen Steuerung dessen Schalter `input_boolean.sg_winter_aktiviert` ausschalten und prüfen, dass `input_boolean.sg_winter_uebernommen` aus ist. Unter **Optionen** lässt sich zusätzlich eine konkurrierende Automation als Sperre auswählen. Solange sie nicht als ausgeschaltet gemeldet wird, bleibt die neue Steuerung inaktiv. Das vorhandene Modbus-Paket kann weiterhin die eigentlichen Schreib- und Rückmelde-Entitäten bereitstellen. Vor dem Entfernen der Integration auf Vorschau wechseln und die Freigabe am Wechselrichter prüfen. Bei vollständig ausgefallenem Home Assistant erfolgt die Freigabe erst, wenn die Verbindung und die Integration wieder laufen.

