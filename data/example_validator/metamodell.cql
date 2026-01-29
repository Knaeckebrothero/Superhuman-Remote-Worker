////////////////////////////////////////////////////////////////////////
// FINIUS Graph-RAG Metamodel
// Version: 2.0
// Updated: January 2026
// Based on: masterplan.md v1.1
////////////////////////////////////////////////////////////////////////

////////////////////////////////////////////////////////////////////////
// 1) KNOTEN-TYPEN (Labels) & ihre Pflichtattribute
////////////////////////////////////////////////////////////////////////

// Anforderungen
CREATE CONSTRAINT IF NOT EXISTS FOR (r:Requirement)
REQUIRE r.rid IS UNIQUE;          // stabile, projektspezifische ID (z. B. R-0001)
CREATE INDEX IF NOT EXISTS FOR (r:Requirement)
ON (r.name);
CREATE INDEX IF NOT EXISTS FOR (r:Requirement)
ON (r.complianceStatus);

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
// 2) RELATIONSTYPEN
////////////////////////////////////////////////////////////////////////

// ── Requirement ↔ Requirement (Strukturierung/Abhängigkeiten) ─────────────────
// r1 REFINES r0    → fachliche Verfeinerung/Herunterbrechen
// r1 DEPENDS_ON r0 → fachliche Abhängigkeit (Reihenfolge/Risikokette)
// r1 TRACES_TO r0  → Traceability (z. B. zu übergeordneten Richtlinien/Policies)
// r1 SUPERSEDES r0 → r1 ersetzt/überholt r0 (Versionierung)

// ── Requirement ↔ BusinessObject (Bezug & Auswirkung) ─────────────────────────
// r RELATES_TO_OBJECT b     → Anforderung bezieht sich auf ein Geschäftsobjekt
// r IMPACTS_OBJECT b        → Umsetzung hat Einfluss auf Struktur/Qualität/GoBD
// r FULFILLED_BY_OBJECT b   → BusinessObject erfüllt diese Anforderung
// r NOT_FULFILLED_BY_OBJECT b → BusinessObject erfüllt diese Anforderung NICHT (Gap)

// ── Requirement ↔ Message (Bezug & Auswirkung) ────────────────────────────────
// r RELATES_TO_MESSAGE m     → Anforderung bezieht sich auf eine Nachricht
// r IMPACTS_MESSAGE m        → Umsetzung verändert Erzeugung/Inhalt/Aufbewahrung
// r FULFILLED_BY_MESSAGE m   → Message erfüllt diese Anforderung
// r NOT_FULFILLED_BY_MESSAGE m → Message erfüllt diese Anforderung NICHT (Gap)

// ── Message ↔ BusinessObject ─────────────────────────────────────────────────
// m USES_OBJECT b     → Nachricht transportiert Daten dieses Geschäftsobjekts
// m PRODUCES_OBJECT b → Nachricht erzeugt Instanz/Effekt (z. B. Buchung angelegt)

////////////////////////////////////////////////////////////////////////
// 3) REQUIREMENT NODE PROPERTIES
////////////////////////////////////////////////////////////////////////
//
// Required Properties:
//   rid: STRING           - Unique requirement ID (e.g., R-0001)
//   name: STRING          - Short descriptive name
//   text: STRING          - Full requirement text
//
// Standard Properties:
//   type: STRING          - 'functional' | 'compliance' | 'constraint' | 'quality'
//   priority: STRING      - 'high' | 'medium' | 'low'
//   status: STRING        - 'draft' | 'active' | 'deprecated' | 'superseded'
//   source: STRING        - Source document name
//   valueStream: STRING   - Associated value stream
//   goBDRelevant: BOOLEAN - GoBD relevance flag
//   gdprRelevant: BOOLEAN - GDPR relevance flag
//   complianceStatus: STRING - 'open' | 'partial' | 'fulfilled'
//   createdAt: DATETIME   - Creation timestamp
//   updatedAt: DATETIME   - Last update timestamp
//   createdBy: STRING     - 'creator_agent' | 'manual' | 'import'
//   validatedBy: STRING   - 'validator_agent' | 'manual'
//   validatedAt: DATETIME - Validation timestamp
//
// Citation Properties:
//   citationIds: LIST<STRING> - References to Citation Engine citations

////////////////////////////////////////////////////////////////////////
// 4) RELATIONSHIP PROPERTIES
////////////////////////////////////////////////////////////////////////

// FULFILLED_BY_OBJECT / FULFILLED_BY_MESSAGE Properties:
// {
//     confidence: FLOAT,        // 0.0-1.0 - How confident is the fulfillment
//     evidence: STRING,         // Description of how it's fulfilled
//     citationId: STRING,       // Reference to Citation Engine citation
//     validatedAt: DATETIME,    // When validation occurred
//     validatedByAgent: STRING  // 'validator_agent' | 'manual'
// }

// NOT_FULFILLED_BY_OBJECT / NOT_FULFILLED_BY_MESSAGE Properties:
// {
//     gapDescription: STRING,   // What's missing
//     severity: STRING,         // 'critical' | 'major' | 'minor'
//     remediation: STRING,      // Suggested fix/action
//     citationId: STRING,       // Reference to Citation Engine citation
//     validatedAt: DATETIME,    // When validation occurred
//     validatedByAgent: STRING  // 'validator_agent' | 'manual'
// }

// SUPERSEDES Properties:
// {
//     reason: STRING,           // Why the requirement was superseded
//     effectiveDate: DATETIME,  // When the supersession takes effect
//     citationId: STRING        // Reference to Citation Engine citation
// }

// General Relationship Properties (applicable to all):
// {
//     rationale: STRING,        // Reasoning for the relationship
//     introducedIn: STRING,     // Version/iteration introduced
//     changedIn: STRING,        // Version/iteration last changed
//     goBDRelevant: BOOLEAN     // GoBD relevance flag
// }

////////////////////////////////////////////////////////////////////////
// 5) KONSISTENZABFRAGEN (Quality Gates)
////////////////////////////////////////////////////////////////////////

// Q1: Anforderungen ohne Bezug zu BO oder Message → handlungsarm
// MATCH (r:Requirement)
// WHERE NOT (r)--(:BusinessObject) AND NOT (r)--(:Message)
// RETURN r.rid AS requirement, r.name AS name LIMIT 100;

// Q2: Messages ohne zugehöriges Geschäftsobjekt → Inhalt unklar
// MATCH (m:Message)
// WHERE NOT (m)-[:USES_OBJECT]-(:BusinessObject)
// RETURN m.mid AS message, m.name AS name LIMIT 100;

// Q3: GOBD-relevante Anforderungen und betroffene Elemente
// MATCH (r:Requirement {goBDRelevant:true})
// OPTIONAL MATCH (r)-[:IMPACTS_OBJECT]->(b:BusinessObject)
// OPTIONAL MATCH (r)-[:IMPACTS_MESSAGE]->(m:Message)
// RETURN r.rid, r.name, collect(DISTINCT b.name) AS objects, collect(DISTINCT m.name) AS messages;

// Q4: Unfulfilled Requirements (complianceStatus = 'open')
// MATCH (r:Requirement)
// WHERE r.complianceStatus = 'open' OR r.complianceStatus IS NULL
// OPTIONAL MATCH (r)-[gap:NOT_FULFILLED_BY_OBJECT]->(b:BusinessObject)
// OPTIONAL MATCH (r)-[msgGap:NOT_FULFILLED_BY_MESSAGE]->(m:Message)
// RETURN r.rid, r.name, r.complianceStatus,
//        collect(DISTINCT {object: b.name, gap: gap.gapDescription, severity: gap.severity}) AS objectGaps,
//        collect(DISTINCT {message: m.name, gap: msgGap.gapDescription, severity: msgGap.severity}) AS messageGaps;

// Q5: Fulfillment Coverage Report
// MATCH (r:Requirement)
// OPTIONAL MATCH (r)-[:FULFILLED_BY_OBJECT]->(fulfilledBy:BusinessObject)
// OPTIONAL MATCH (r)-[:NOT_FULFILLED_BY_OBJECT]->(notFulfilledBy:BusinessObject)
// RETURN r.rid, r.name, r.complianceStatus,
//        COUNT(DISTINCT fulfilledBy) AS fulfilledCount,
//        COUNT(DISTINCT notFulfilledBy) AS gapCount,
//        CASE
//          WHEN COUNT(DISTINCT notFulfilledBy) = 0 AND COUNT(DISTINCT fulfilledBy) > 0 THEN 'fulfilled'
//          WHEN COUNT(DISTINCT notFulfilledBy) > 0 AND COUNT(DISTINCT fulfilledBy) > 0 THEN 'partial'
//          ELSE 'open'
//        END AS calculatedStatus;

////////////////////////////////////////////////////////////////////////
// 6) VORLAGEN (Templates) – sichere MERGE-Patterns
////////////////////////////////////////////////////////////////////////

// Requirement anlegen (with complianceStatus)
// MERGE (r:Requirement {rid:$rid})
//   ON CREATE SET r.name=$name, r.text=$text, r.type=$type, r.priority=$priority,
//                 r.status=$status, r.source=$source, r.valueStream=$valueStream,
//                 r.goBDRelevant=coalesce($goBDRelevant,false),
//                 r.gdprRelevant=coalesce($gdprRelevant,false),
//                 r.complianceStatus='open',
//                 r.createdAt=timestamp(), r.updatedAt=timestamp(),
//                 r.createdBy=$createdBy
//   ON MATCH  SET r.name=$name, r.text=$text, r.type=$type, r.priority=$priority,
//                 r.status=$status, r.source=$source, r.valueStream=$valueStream,
//                 r.goBDRelevant=coalesce($goBDRelevant,r.goBDRelevant),
//                 r.gdprRelevant=coalesce($gdprRelevant,r.gdprRelevant),
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

// Standard Relationen
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

// Fulfillment Relationen (NEU in v2.0)
// MATCH (r:Requirement {rid:$rid}),(b:BusinessObject {boid:$boid})
// MERGE (r)-[rel:FULFILLED_BY_OBJECT]->(b)
//   ON CREATE SET rel.confidence=$confidence, rel.evidence=$evidence,
//                 rel.citationId=$citationId, rel.validatedAt=timestamp(),
//                 rel.validatedByAgent=$validatedByAgent;

// MATCH (r:Requirement {rid:$rid}),(b:BusinessObject {boid:$boid})
// MERGE (r)-[rel:NOT_FULFILLED_BY_OBJECT]->(b)
//   ON CREATE SET rel.gapDescription=$gapDescription, rel.severity=$severity,
//                 rel.remediation=$remediation, rel.citationId=$citationId,
//                 rel.validatedAt=timestamp(), rel.validatedByAgent=$validatedByAgent;

// MATCH (r:Requirement {rid:$rid}),(m:Message {mid:$mid})
// MERGE (r)-[rel:FULFILLED_BY_MESSAGE]->(m)
//   ON CREATE SET rel.confidence=$confidence, rel.evidence=$evidence,
//                 rel.citationId=$citationId, rel.validatedAt=timestamp(),
//                 rel.validatedByAgent=$validatedByAgent;

// MATCH (r:Requirement {rid:$rid}),(m:Message {mid:$mid})
// MERGE (r)-[rel:NOT_FULFILLED_BY_MESSAGE]->(m)
//   ON CREATE SET rel.gapDescription=$gapDescription, rel.severity=$severity,
//                 rel.remediation=$remediation, rel.citationId=$citationId,
//                 rel.validatedAt=timestamp(), rel.validatedByAgent=$validatedByAgent;

// Requirement-Hierarchien
// MATCH (p:Requirement {rid:$parentRid}),(c:Requirement {rid:$childRid})
// MERGE (c)-[:REFINES]->(p);

// MATCH (a:Requirement {rid:$a}),(b:Requirement {rid:$b})
// MERGE (a)-[:DEPENDS_ON]->(b);

// MATCH (old:Requirement {rid:$oldRid}),(new:Requirement {rid:$newRid})
// MERGE (new)-[rel:SUPERSEDES]->(old)
//   ON CREATE SET rel.reason=$reason, rel.effectiveDate=timestamp(),
//                 rel.citationId=$citationId;

////////////////////////////////////////////////////////////////////////
// 7) COMPLIANCE STATUS UPDATE PROCEDURE
////////////////////////////////////////////////////////////////////////
// After adding fulfillment relationships, update complianceStatus:
//
// MATCH (r:Requirement {rid:$rid})
// OPTIONAL MATCH (r)-[:FULFILLED_BY_OBJECT|FULFILLED_BY_MESSAGE]->(fulfilled)
// OPTIONAL MATCH (r)-[:NOT_FULFILLED_BY_OBJECT|NOT_FULFILLED_BY_MESSAGE]->(notFulfilled)
// WITH r, COUNT(DISTINCT fulfilled) AS fulfilledCount, COUNT(DISTINCT notFulfilled) AS gapCount
// SET r.complianceStatus = CASE
//   WHEN gapCount = 0 AND fulfilledCount > 0 THEN 'fulfilled'
//   WHEN gapCount > 0 AND fulfilledCount > 0 THEN 'partial'
//   ELSE 'open'
// END,
// r.updatedAt = timestamp()
// RETURN r.rid, r.complianceStatus;

////////////////////////////////////////////////////////////////////////
// 8) BEISPIEL (Dummy) – bitte im Projekt ersetzen
////////////////////////////////////////////////////////////////////////
// MERGE (:BusinessObject {boid:'BO-Rechnung', name:'Rechnung', domain:'Accounting'});
// MERGE (:BusinessObject {boid:'BO-Zahlung',  name:'Zahlung',  domain:'Payments'});
// MERGE (:Message {mid:'MSG-ReservationRequest', name:'ReservationRequest', direction:'inbound', format:'JSON'});
// MERGE (:Requirement {rid:'R-0001', name:'Rechnung erzeugen', type:'functional', goBDRelevant:true, complianceStatus:'open', text:'Bei Mietende wird eine Rechnung erzeugt.'});
// MATCH (r:Requirement {rid:'R-0001'}),(bo:BusinessObject {boid:'BO-Rechnung'})
// MERGE (r)-[:IMPACTS_OBJECT {goBDRelevant:true,rationale:'GoBD Nachvollziehbarkeit'}]->(bo);
// MATCH (m:Message {mid:'MSG-ReservationRequest'}),(bo:BusinessObject {boid:'BO-Rechnung'})
// MERGE (m)-[:USES_OBJECT]->(bo);


RETURN 'metamodel v2.0 seeded' AS status;
