////////////////////////////////////////////////////////////////////////
// 1) KNOTEN-TYPEN (Labels) & ihre Pflichtattribute
////////////////////////////////////////////////////////////////////////

// Anforderungen
CREATE CONSTRAINT IF NOT EXISTS FOR (r:Requirement)
REQUIRE r.rid IS UNIQUE;          // stabile, projektspezifische ID (z. B. R-0001)
CREATE INDEX IF NOT EXISTS FOR (r:Requirement)
ON (r.name);

// Geschäftsobjekte
CREATE CONSTRAINT IF NOT EXISTS FOR (b:BusinessObject)
REQUIRE b.boid IS UNIQUE;         // stabile ID (z. B. BO-Reservierung)
CREATE INDEX IF NOT EXISTS FOR (b:BusinessObject)
ON (b.name);

// Nachrichten
CREATE CONSTRAINT IF NOT EXISTS FOR (m:Message)
REQUIRE m.mid IS UNIQUE;          // stabile ID (z. B. MSG-ReservationRequest)
CREATE INDEX IF NOT EXISTS FOR (m:Message)
ON (m.name);

////////////////////////////////////////////////////////////////////////
// 2) RELATIONSTYPEN (nur zwischen Requirement, BusinessObject, Message)
////////////////////////////////////////////////////////////////////////
// ── Requirement ↔ Requirement (Strukturierung/Abhängigkeiten) ─────────────────
// r1 REFINES r0   → fachliche Verfeinerung/Herunterbrechen
// r1 DEPENDS_ON r0→ fachliche Abhängigkeit (Reihenfolge/Risikokette)
// r1 TRACES_TO r0 → Traceability (z. B. zu übergeordneten Richtlinien/Policies)

// ── Requirement ↔ BusinessObject ─────────────────────────────────────────────
// r RELATES_TO_OBJECT b → Anforderung bezieht sich auf ein Geschäftsobjekt
// r IMPACTS_OBJECT  b   → Umsetzung hat Einfluss auf Struktur/Qualität/GoBD-Aspekte

// ── Requirement ↔ Message ────────────────────────────────────────────────────
// r RELATES_TO_MESSAGE m → Anforderung bezieht sich auf eine Nachricht
// r IMPACTS_MESSAGE  m   → Umsetzung verändert Erzeugung/Inhalt/Aufbewahrung/GoBD

// ── Message ↔ BusinessObject ─────────────────────────────────────────────────
// m USES_OBJECT b     → Nachricht transportiert Daten dieses Geschäftsobjekts
// m PRODUCES_OBJECT b → Nachricht erzeugt Instanz/Effekt (z. B. Buchung angelegt)

////////////////////////////////////////////////////////////////////////
// 3) RELATIONSPROPERTY-CONVENTIONS (Neo4j 5.x – per Konvention, optional indexiert)
////////////////////////////////////////////////////////////////////////
// Beispiel-Properties: { rationale, introducedIn, changedIn, goBDRelevant:boolean }

////////////////////////////////////////////////////////////////////////
// 4) MINIMAL-SET: KONSISTENZABFRAGEN (Quality Gates für Impact-Analyse)
////////////////////////////////////////////////////////////////////////
// Q1: Anforderungen ohne Bezug zu BO oder Message → handlungsarm, Ergänzung nötig
// MATCH (r:Requirement)
// WHERE NOT (r)--(:BusinessObject) AND NOT (r)--(:Message)
// RETURN r.rid AS requirement, r.name AS name LIMIT 100;

// Q2: Messages ohne zugehöriges Geschäftsobjekt → Inhalt unklar
// MATCH (m:Message)
// WHERE NOT (m)-[:USES_OBJECT]-(:BusinessObject)
// RETURN m.mid AS message, m.name AS name LIMIT 100;

// Q3: GOBD-relevante Anforderungen (per Flag) und betroffene Elemente
// MATCH (r:Requirement {goBDRelevant:true})
// OPTIONAL MATCH (r)-[:IMPACTS_OBJECT]->(b:BusinessObject)
// OPTIONAL MATCH (r)-[:IMPACTS_MESSAGE]->(m:Message)
// RETURN r.rid, r.name, collect(DISTINCT b.name) AS objects, collect(DISTINCT m.name) AS messages;

////////////////////////////////////////////////////////////////////////
// 5) VORLAGEN (Templates) – sichere MERGE-Patterns für Import/ETL
////////////////////////////////////////////////////////////////////////

// Requirement anlegen
// MERGE (r:Requirement {rid:$rid})
//   ON CREATE SET r.name=$name, r.text=$text, r.type=$type, r.priority=$priority,
//                 r.status=$status, r.source=$source, r.valueStream=$valueStream,
//                 r.goBDRelevant=coalesce($goBDRelevant,false),
//                 r.createdAt=timestamp(), r.updatedAt=timestamp()
//   ON MATCH  SET r.name=$name, r.text=$text, r.type=$type, r.priority=$priority,
//                 r.status=$status, r.source=$source, r.valueStream=$valueStream,
//                 r.goBDRelevant=coalesce($goBDRelevant,r.goBDRelevant),
//                 r.updatedAt=timestamp();

// BusinessObject anlegen
// MERGE (b:BusinessObject {boid:$boid})
//   ON CREATE SET b.name=$name, b.description=$description, b.domain=$domain,
//                 b.owner=$owner, b.createdAt=timestamp(), b.updatedAt=timestamp()
//   ON MATCH  SET b.name=$name, b.description=$description, b.domain=$domain,
//                 b.owner=$owner, b.updatedAt=timestamp();

// Message anlegen
// MERGE (m:Message {mid:$mid})
//   ON CREATE SET m.name=$name, m.description=$description, m.direction=$direction,
//                 m.format=$format, m.protocol=$protocol, m.version=$version,
//                 m.createdAt=timestamp(), m.updatedAt=timestamp()
//   ON MATCH  SET m.name=$name, m.description=$description, m.direction=$direction,
//                 m.format=$format, m.protocol=$protocol, m.version=$version,
//                 m.updatedAt=timestamp();

// Relationen
// MATCH (r:Requirement {rid:$rid}),(b:BusinessObject {boid:$boid})
// MERGE (r)-[rel:RELATES_TO_OBJECT]->(b)
//   ON CREATE SET rel.rationale=$rationale, rel.introducedIn=$introducedIn;

// MATCH (r:Requirement {rid:$rid}),(b:BusinessObject {boid:$boid})
// MERGE (r)-[rel:IMPACTS_OBJECT]->(b)
//   ON CREATE SET rel.goBDRelevant=coalesce($goBDRelevant,true), rel.rationale=$rationale;

// MATCH (r:Requirement {rid:$rid}),(m:Message {mid:$mid})
// MERGE (r)-[rel:IMPACTS_MESSAGE]->(m)
//   ON CREATE SET rel.goBDRelevant=coalesce($goBDRelevant,true), rel.rationale=$rationale;

// MATCH (m:Message {mid:$mid}),(b:BusinessObject {boid:$boid})
// MERGE (m)-[rel:USES_OBJECT]->(b)
//   ON CREATE SET rel.rationale=$rationale;

// Requirement-Hierarchien
// MATCH (p:Requirement {rid:$parentRid}),(c:Requirement {rid:$childRid})
// MERGE (c)-[:REFINES]->(p);
// MATCH (a:Requirement {rid:$a}),(b:Requirement {rid:$b})
// MERGE (a)-[:DEPENDS_ON]->(b);

////////////////////////////////////////////////////////////////////////
// 6) BEISPIEL (Dummy) – bitte im Projekt ersetzen
////////////////////////////////////////////////////////////////////////
// MERGE (:BusinessObject {boid:'BO-Rechnung', name:'Rechnung', domain:'Accounting'});
// MERGE (:BusinessObject {boid:'BO-Zahlung',  name:'Zahlung',  domain:'Payments'});
// MERGE (:Message {mid:'MSG-ReservationRequest', name:'ReservationRequest', direction:'inbound', format:'JSON'});
// MERGE (:Requirement {rid:'R-0001', name:'Rechnung erzeugen', type:'functional', goBDRelevant:true, text:'Bei Mietende wird eine Rechnung erzeugt.'});
// MATCH (r:Requirement {rid:'R-0001'}),(bo:BusinessObject {boid:'BO-Rechnung'})
// MERGE (r)-[:IMPACTS_OBJECT {goBDRelevant:true,rationale:'GoBD Nachvollziehbarkeit'}]->(bo);
// MATCH (m:Message {mid:'MSG-ReservationRequest'}),(bo:BusinessObject {boid:'BO-Rechnung'})
// MERGE (m)-[:USES_OBJECT]->(bo);


RETURN 'metamodel seeded' AS status;