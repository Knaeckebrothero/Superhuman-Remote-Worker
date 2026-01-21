-- PostgreSQL Seed Data
-- Generated for Graph-RAG validator testing
-- Exported: 2026-01-21T10:42:19.765191
--
-- This file contains INSERT statements for seeding the database.
-- Run after schema.sql to populate with test data.
-- Tables: jobs, requirements, sources, citations

-- ============================================================================
-- JOBS
-- ============================================================================

INSERT INTO jobs ("id", "prompt", "document_path", "document_content", "context", "status", "creator_status", "validator_status", "created_at", "updated_at", "completed_at", "error_message", "error_details", "total_tokens_used", "total_requests") VALUES ('bad7b675-da31-439f-a32e-e9505c8ab308', 'Identify possible requirements for a medium sized car rental company based on the provided GoBD document.', '/home/ghost/Repositories/Uni-Projekt-Graph-RAG/data/example_data/GoBD.pdf', NULL, '{}', 'completed', 'pending', 'pending', '2026-01-16T16:55:09.648580+00:00', '2026-01-16T21:25:52.387320+00:00', '2026-01-16T21:25:52.387320+00:00', NULL, NULL, 0, 0);

-- 1 job(s) exported

-- ============================================================================
-- REQUIREMENTS
-- ============================================================================

INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('c47bb370-a801-4ae0-a00a-d1f4844cc668', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'GoBD-11-RecordDetail', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 11"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 11.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:31:53.783579+00:00', '2026-01-16T17:31:53.783579+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('4bc3d8b1-99a6-4caf-8992-ae78c6c84d9d', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Branch‑specific minimal recording obligations and reasonableness must be considered.', 'GoBD-11-BranchSpecificObligations', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 11"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 11.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:33:20.363381+00:00', '2026-01-16T17:33:20.363381+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('ccdd8d09-0836-4afa-91d7-52c050e212e2', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'GoBD-11-RecordDetail', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 11"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 11.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:33:38.052275+00:00', '2026-01-16T17:33:38.052275+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('d257f511-4d31-4121-a910-e5f79749257f', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'The recording of each individual transaction is not required if it is technically, economically, and practically impossible, and the taxpayer must prove this.', 'GoBD-11-TransactionImpossibility', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 11"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 11.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:34:06.049829+00:00', '2026-01-16T17:34:06.049829+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('7ab7e49a-977a-4e82-b1dd-16f799b0ff25', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'For cash sales to many unknown persons, the individual recording requirement does not apply if an open cash register is used; however, if an electronic recording system is used, the individual recording requirement applies regardless of technical security.', 'GoBD-11-CashSalesRecording', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 11"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 11.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:35:02.495042+00:00', '2026-01-16T17:35:02.495042+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('56e36663-53d8-4bad-86b2-f37556f801de', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'The completeness and traceability of all business transactions must be ensured in IT systems through technical and organizational controls.', 'GoBD-11-CompletenessTraceability', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 11"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 11.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:35:28.122616+00:00', '2026-01-16T17:35:28.122616+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('73819718-4af6-46f2-b275-2b6971eb98b8', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'A single business transaction must not be recorded multiple times.', 'GoBD-12-UniqueTransaction', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 12"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 12.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:36:41.054631+00:00', '2026-01-16T17:36:41.054631+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('3d6359c8-8d7f-41eb-8372-5f5175fa5411', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Plausibility controls must be implemented for data entry, including content plausibility checks, automated assignment of record numbers, gap analysis, and duplicate analysis for document numbers.', 'GoBD-11-PlausibilityControls', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 12"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 12.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:37:15.468604+00:00', '2026-01-16T17:37:15.468604+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('7e4bbce1-65f4-4571-9187-83a8a67bb4a5', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Summarized or aggregated records in the general ledger are permissible only if they can be traced back to individual entries in the underlying records.', 'GoBD-12-AggregatedRecords', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 12"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 12.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:38:39.389224+00:00', '2026-01-16T17:38:39.389224+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('0dfd62c0-3059-442c-9c45-b4256a149de3', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'The recording or processing of actual business transactions must not be suppressed; e.g., a receipt or invoice must not be issued without recording cash received.', 'GoBD-12-TransactionSuppression', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 12"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 12.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:39:00.817822+00:00', '2026-01-16T17:39:00.817822+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('e9bb3c5d-ac2d-4cdb-a4ac-ecfb5672e03b', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'The entrepreneur must record for each transaction the unit price, valuation date, discount information, payment method, and tax exemption details.', 'GoBD-21-TransactionDetails', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 21"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 21.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:40:08.019916+00:00', '2026-01-16T17:40:08.019916+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('1ace4d0b-1d5d-40b7-9263-4ffedaf88e83', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Business transactions must be recorded truthfully and in accordance with actual circumstances and legal provisions.', 'GoBD-13-TruthfulRecording', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 13"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 13.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:40:28.086167+00:00', '2026-01-16T17:40:28.086167+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('98f5abac-4e14-48ba-bcba-98876586d209', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'The taxpayer must document internal program rules for generating bookings, ensure they are subject to authorized change procedures, provide evidence of the approved procedure''s application, and evidence of actual execution of each booking.', 'GoBD-21-ProgramRules', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 21"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 21.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:42:00.282774+00:00', '2026-01-16T17:42:00.282774+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('907da237-11d3-4860-8613-03837ec35c91', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Original source documents for recurring transactions must be retained for the purpose of automatic bookings.', 'GoBD-21-RecurringSourceDocs', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 21"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 21.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:45:46.882951+00:00', '2026-01-16T17:45:46.882951+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('484788a7-a808-4d8d-8d25-ca7a5b4e88a9', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'The entrepreneur must record for each transaction the unit price, valuation date, discount information, payment method, and tax exemption details.', 'GoBD-21-UnitPriceDetails', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 21"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 21.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:46:02.480726+00:00', '2026-01-16T17:46:02.480726+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('af59c6a1-34b3-4f29-a787-2f926301e602', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'The taxpayer must ensure that electronic bookings and records are made individually, completely, correctly, timely, and orderly, and each must be linked to a supporting document.', 'GoBD-21-ElectronicBookingsLinking', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 21"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 21.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:46:22.388771+00:00', '2026-01-16T17:46:22.388771+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('f3d02921-b86e-4751-98fa-a14a67182c90', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'All business transactions must be recorded in chronological order and in a logical classification.', 'GoBD-22-ChronologicalOrder', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 22"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 22.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:47:24.585123+00:00', '2026-01-16T17:47:24.585123+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('9c2b7ca9-6cd0-4f18-8079-9a6f072a1946', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Each business transaction must result in at least a double‑entry booking (debit and credit).', 'GoBD-22-DoubleEntryBooking', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 22"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 22.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:47:48.137148+00:00', '2026-01-16T17:47:48.137148+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('5468bb93-3420-460c-8f2e-898cff6e0c9d', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'The organization must maintain the integrity of the initial recording of business transactions throughout subsequent processes.', 'GoBD-22-IntegrityInitialRecording', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 22"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 22.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:49:21.863200+00:00', '2026-01-16T17:49:21.863200+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('f1bcb663-978a-4fe8-8b05-16d091a560dd', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Business transactions must be continuously recorded in either paper form or electronic primary records to ensure document security and non‑loss.', 'GoBD-22-ContinuousRecording', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 22"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 22.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:55:55.707230+00:00', '2026-01-16T17:55:55.707230+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('5cd31c4b-3ceb-47fa-8f79-4b171b7001bf', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'The recorded data must include the entry date (if different from booking date) and a fixed record indicator, ensuring immutability unless automatically ensured.', 'GoBD-22-EntryDateFixedIndicator', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 22"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 22.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:58:39.963337+00:00', '2026-01-16T17:58:39.963337+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('459d69f3-d06b-4375-a519-3f0c265d620e', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Controls must be in place to ensure that all business transactions are fully captured and cannot be altered without authorization.', 'GoBD-23-ControlsFullCapture', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 23"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 23.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T17:59:21.760459+00:00', '2026-01-16T17:59:21.760459+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('61252499-e665-4d9f-a127-b18743268f66', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Mathematisch‑technische Auswertungen müssen automatisiert (DV‑gestützt) interpretiert, dargestellt, verarbeitet und für andere Datenbank‑anwendungen und Prüfsoftware nutzbar gemacht werden, ohne weitere Konvertierungs‑ und Bearbeitungsschritte und ohne Informationsverlust.', 'GoBD-31-Req1', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 31"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 31.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T18:02:45.461237+00:00', '2026-01-16T18:02:45.461237+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('1cd42573-8241-44b2-ac8c-9cae1ae7551f', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Mathematisch‑technische Auswertungen sind bei elektronischen Grund(buch)aufzeichnungen, Journaldaten und strukturierten Text‑ bzw. Tabellendateien möglich.', 'GoBD-31-Req2', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 31"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 31.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T18:03:19.376880+00:00', '2026-01-16T18:03:19.376880+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('03449efc-9738-45e5-b279-998909c01f0c', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Alle zur maschinellen Auswertung notwendigen Strukturinformationen (Dateiherkunft, Dateistruktur, Datenfelder, Zeichensatztabellen) sowie interne und externe Verknüpfungen müssen in maschinell auswertbarer Form aufbewahrt werden.', 'GoBD-31-Req3', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 31"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 31.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T18:03:35.403583+00:00', '2026-01-16T18:03:35.403583+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('f889bc9c-ee19-4185-a505-b9b49a3796da', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Der Erhalt technischer Verlinkungen auf dem Datenträger ist nicht erforderlich, sofern dies nicht möglich ist.', 'GoBD-31-Req4', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 31"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 31.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T18:04:06.276680+00:00', '2026-01-16T18:04:06.276680+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('838141a2-f736-414d-947f-940d6475be19', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Die Reduzierung einer bestehenden maschinellen Auswertbarkeit durch Umwandlung des Dateiformats oder Auswahl bestimmter Aufbewahrungsformen ist nicht zulässig.', 'GoBD-31-Req5', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 31"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 31.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T18:04:43.457936+00:00', '2026-01-16T18:04:43.457936+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('33d36601-a706-4501-8fa2-786e69c8b35d', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Formatumwandlungen (z. B. PDF/A → Bildformat) sind nur zulässig, wenn die maschinelle Auswertbarkeit nicht eingeschränkt wird und keine inhaltliche Veränderung vorgenommen wird.', 'GoBD-31-Req6', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 31"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 31.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T18:05:08.234638+00:00', '2026-01-16T18:05:08.234638+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('73cd2d07-9855-4937-a74d-6392fdbae334', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Der Steuerpflichtige muss berücksichtigen, dass Einschränkungen bei der Speicherung von E‑Mails als PDF zu Lasten des Steuerpflichtigen gehen können.', 'GoBD-32-Req1', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 32"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 32.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T18:05:31.617909+00:00', '2026-01-16T18:05:31.617909+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('5da7ad5a-5387-4d9c-bf0c-70cde3e79038', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Handels‑ und Geschäftsbriefe sowie Buchungsbelege, die in Papierform empfangen und elektronisch erfasst werden, müssen so aufbewahrt werden, dass das elektronische Dokument mit dem Original bildlich übereinstimmt.', 'GoBD-32-Req2', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 32"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 32.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T18:06:53.875847+00:00', '2026-01-16T18:06:53.875847+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('e0d7f2ab-4df0-4a26-b2e3-70a1c291ad00', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Bei bildlicher Erfassung mit OCR muss der Volltext nach Verifikation und Korrektur über die Aufbewahrungsfrist hinweg aufbewahrt und für Prüfzwecke verfügbar sein.', 'GoBD-32-Req3', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 32"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD regulation page 32.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T18:08:08.965902+00:00', '2026-01-16T18:08:08.965902+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('1af770fe-88ba-4679-81cd-c24af577cfa5', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Ein und derselbe Geschäftsvorfall darf nicht mehrfach aufgezeichnet werden.', 'GoBD-11-UniqueTransactionRecording', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 11"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf pages 11-20 as a requirement to avoid duplicate recording of business transactions.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:15:30.614904+00:00', '2026-01-16T19:15:30.614904+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('f505f556-2baa-43e9-96c4-b15186a6b4bc', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Die vollständige und lückenlose Erfassung und Wiedergabe aller Geschäftsvorfälle ist bei DV-Systemen durch ein Zusammenspiel von technischen (einschließlich programmierten) und organisatorischen Kontrollen sicherzustellen (z. B. Erfassungskontrollen, Plausibilitätskontrollen bei Dateneingaben, inhaltliche Plausibilitätskontrollen, automatisierte Vergabe von Datensatznummern, Lückenanalyse oder Mehrfachbelegungsanalyse bei Belegnummern).', 'GoBD-11-CompleteTransactionRecording', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 11"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf pages 11-20 as a requirement for complete and gapless recording of all business transactions in IT systems, ensuring technical and organizational controls.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:16:27.340734+00:00', '2026-01-16T19:16:27.340734+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('6ca040b4-6dc6-4428-ada8-cf8d6fabc843', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Zusammengefasste oder verdichtete Aufzeichnungen im Hauptbuch (Konto) sind zulässig, sofern sie nachvollziehbar in ihre Einzelpositionen in den Grund(buch)aufzeichnungen oder des Journals aufgegliedert werden können. Andernfalls ist die Nachvollziehbarkeit und Nachprüfbarkeit nicht gewährleistet.', 'GoBD-12-AggregatedRecords', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 12"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf pages 11-20 as a requirement for allowing aggregated records in the main ledger only if they can be traced back to individual entries, ensuring traceability and auditability.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:16:48.912961+00:00', '2026-01-16T19:16:48.912961+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('06928a13-b75b-4267-bef0-c95e56e4fdf4', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Die Erfassung oder Verarbeitung von tatsächlichen Geschäftsvorfällen darf nicht unterdrückt werden.', 'GoBD-12-TransactionRecordingProhibition', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 12"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf pages 11-20 as a requirement that the recording or processing of actual business transactions must not be suppressed.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:18:09.918551+00:00', '2026-01-16T19:18:09.918551+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('5acc1e62-05d0-4e83-ab61-6dc9f957d046', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Geschäftsvorfälle sind in Übereinstimmung mit den tatsächlichen Verhältnissen und im Einklang mit den rechtlichen Vorschriften inhaltlich zutreffend durch Belege abzubilden.', 'GoBD-12-AccurateTransactionRepresentation', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 12"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf pages 11-20 as a requirement that business transactions must be accurately represented by documents in accordance with actual circumstances and legal regulations.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:19:00.384270+00:00', '2026-01-16T19:19:00.384270+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('a633c0d6-c074-483d-8b22-677e576511ac', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Das Erfordernis „zeitgerecht“ zu buchen verlangt, dass ein zeitlicher Zusammenhang zwischen den Vorgängen und ihrer buchmäßigen Erfassung besteht.', 'GoBD-12-TimelyBookingRequirement', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 12"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf pages 11-20 as a requirement that bookings must be made in a timely manner, ensuring a temporal link between events and their accounting.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:19:43.736109+00:00', '2026-01-16T19:19:43.736109+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('d5a8666c-c113-4276-9774-9464d637824f', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Jeder Geschäftsvorfall ist zeitnah, d. h. möglichst unmittelbar nach seiner Entstehung in einer Grundaufzeichnung oder in einem Grundbuch zu erfassen.', 'GoBD-12-PromptRecording', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 12"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf pages 11-20 as a requirement that each business transaction must be recorded promptly in a primary record or ledger.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:20:04.272442+00:00', '2026-01-16T19:20:04.272442+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('42fdde3a-69ac-45c2-98f9-1017769dddfc', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Kasseneinnahmen und Kassenausgaben sind nach § 146 Absatz 1 Satz 2 AO täglich festzuhalten.', 'GoBD-12-DailyCashRecording', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 12"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf pages 11-20 as a requirement that cash receipts and payments must be recorded daily according to tax law.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:20:43.095017+00:00', '2026-01-16T19:20:43.095017+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('86e91dc2-6be3-4713-b7a8-8b43aafbf6d2', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Es ist nicht zu beanstanden, wenn Waren- und Kostenrechnungen, die innerhalb von acht Tagen nach Rechnungseingang oder innerhalb der ihrem gewöhnlichen Durchlauf durch den Betrieb entsprechenden Zeit beglichen werden, nicht erfasst werden, sofern die Vollständigkeit der Geschäftsvorfälle im Einzelfall gewährleistet ist.', 'GoBD-12-NonRecordingWithinEightDays', 'functional', 'medium', 'documents/GoBD.pdf', '{"section": "Page 12"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf pages 11-20 as a requirement allowing non-recording of invoices within eight days if completeness is ensured.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:20:57.394288+00:00', '2026-01-16T19:20:57.394288+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('cf0a8085-58b1-4ae4-aebb-fad32c939220', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Jeder Geschäftsvorfall ist periodengerecht der Abrechnungsperiode zuzuordnen, in der er angefallen ist.', 'GoBD-14-PeriodicAssignment', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 14"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 14 as a requirement that each business transaction must be assigned to the correct accounting period.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:21:13.704226+00:00', '2026-01-16T19:21:13.704226+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('cac6227e-8377-4d1f-9c4e-c209d05300bd', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Bei der doppelten Buchführung sind die Buchungen so zu verarbeiten, dass sie geordnet darstellbar sind und innerhalb angemessener Zeit ein Überblick über die Vermögens- und Ertragslage gewährleistet ist.', 'GoBD-14-OrderedBookkeepingProcessing', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 14"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 14 as a requirement that double-entry bookkeeping must process bookings in an ordered manner to provide a timely overview of assets and earnings.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:22:13.045136+00:00', '2026-01-16T19:22:13.045136+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('bad2f6e8-2cb0-4323-a65a-7c123925ae1a', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Eine Buchung oder eine Aufzeichnung darf nicht in einer Weise verändert werden, dass der ursprüngliche Inhalt nicht mehr feststellbar ist.', 'GoBD-15-NoAlterationOfOriginalContent', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 15"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 15 as a requirement that bookings or records must not be altered in a way that the original content becomes unidentifiable, ensuring data integrity.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:22:38.971499+00:00', '2026-01-16T19:22:38.971499+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('2a840dca-6b70-43d1-82fb-1e74ff7244ef', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Der Steuerpflichtige hat organisatorisch und technisch sicherzustellen, dass die elektronischen Buchungen und sonst erforderlichen elektronischen Aufzeichnungen einzeln, vollständig, richtig, zeitgerecht und geordnet vorgenommen werden (§ 146 Absatz 1 Satz 1 AO, § 239 Absatz 2 HGB). Jede Buchung oder Aufzeichnung muss im Zusammenhang mit einem Beleg stehen.', 'GoBD-21-OrganizationalTechnicalEnsuring', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 21"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf pages 21-30 as a requirement to ensure organizational and technical controls for electronic bookings and records, including completeness, correctness, timeliness, and ordering, and linking each entry to a supporting document.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:25:17.107681+00:00', '2026-01-16T19:25:17.107681+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('d51ab65c-3113-462b-96c3-f833761526ff', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Bei der doppelten Buchführung müssen alle Geschäftsvorfälle in zeitlicher Reihenfolge (Grund(buch)aufzeichnung, Journalfunktion) und in sachlicher Gliederung (Hauptbuch, Kontenfunktion) darstellbar sein.', 'GoBD-22-TemporalOrderAndClassification', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 22"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf pages 21-30 as a requirement that double-entry bookkeeping must present all business transactions in chronological order and in proper classification.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:25:58.618157+00:00', '2026-01-16T19:25:58.618157+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('57db8f86-73e8-431e-8074-4fb6474633ef', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Unprotokollierte Änderungen an elektronischen Grund(buch)aufzeichnungen sind nicht zulässig, wenn die Aufzeichnungen die Belegfunktion erfüllen.', 'GoBD-22-UnrecordedChangesProhibited', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 22"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 22 as a requirement that unrecorded changes to electronic primary records are prohibited when they serve the document function.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:26:21.927613+00:00', '2026-01-16T19:26:21.927613+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('20e55a2e-8a09-4304-9afa-6bc717c89295', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Die fortlaufende Aufzeichnung der Geschäftsvorfälle muss zunächst in Papierform oder in elektronischen Grund(buch)aufzeichnungen erfolgen, um die Belegsicherung und die Garantie der Unverlierbarkeit des Geschäftsvorfalls zu gewährleisten.', 'GoBD-22-InitialRecordingInPaperOrElectronic', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 22"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf pages 21-30 as a requirement that continuous recording of business transactions must initially be in paper form or electronic primary records to ensure document security and guarantee non-loss of the transaction.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:30:11.803207+00:00', '2026-01-16T19:30:11.803207+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('ea6f35d2-4bf6-4b0e-aa7d-aaf83d5a98a7', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Zu den aufzeichnungspflichtigen Inhalten gehören das Erfassungsdatum, soweit abweichend vom Buchungsdatum, und die Angabe zwingend (§ 146 Absatz 1 Satz 1 AO, zeitgerecht).', 'GoBD-22-RecordingDateRequirement', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 22"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 22 as a requirement that the recording date (if different from booking date) and mandatory timing information must be recorded for each transaction.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:31:24.501337+00:00', '2026-01-16T19:31:24.501337+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('fda9f1ac-9def-46bd-bf8a-7d00cd09941f', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Maschinell auswertbare Informationen müssen vollständig und in unverdichteter Form aufbewahrt werden, einschließlich aller Strukturinformationen, um die maschinelle Auswertung zu ermöglichen.', 'GoBD-31-MachineReadabilityPreservation', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 31"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 31 as a requirement to retain all data and structure information in machine-readable form for evaluation.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:32:20.996918+00:00', '2026-01-16T19:32:20.996918+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('93796d0d-7989-417e-be78-d2695378d90d', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Die Verbuchung im Journal des Hauptsystems darf bis zum Ablauf des folgenden Monats nicht beanstandet werden, wenn die einzelnen Geschäftsvorfälle bereits in einem Vor- oder Nebensystem die Grundaufzeichnungsfunktion erfüllen und die Einzeldaten aufbewahrt werden.', 'GoBD-23-JournalPostingDeadline', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 23"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 23 as a requirement that journal postings must not be objected to until the end of the following month if the transactions are already recorded in a subsidiary system and data are retained.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:39:29.369680+00:00', '2026-01-16T19:39:29.369680+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('e9f07b48-62f7-480d-b5c2-72a540a3ea16', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Durch Erfassungs-, Übertragungs- und Verarbeitungskontrollen ist sicherzustellen, dass alle Geschäftsvorfälle vollständig erfasst und nicht unbefugt verändert werden können; die Durchführung der Kontrollen ist zu protokollieren.', 'GoBD-23-ProcessingControls', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 23"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 23 as a requirement for processing controls to ensure completeness and integrity of business transactions.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:40:31.216966+00:00', '2026-01-16T19:40:31.216966+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('d4753ccd-c36a-4fcc-b1ee-bea459df4bf0', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Alle für die Verarbeitung erforderlichen Tabellendaten, Stammdaten, Bewegungsdaten, Metadaten, Historisierung und Programme müssen gespeichert und historisiert werden.', 'GoBD-23-DataStorageHistorical', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 23"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 23 as a requirement that all data required for processing, including tables, master data, transaction data, metadata, historization, and programs, must be stored and historized.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:40:47.912478+00:00', '2026-01-16T19:40:47.912478+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('10aa5372-c3f5-4916-8092-b5e5c8306c86', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Die Journalfunktion erfordert eine vollständige, zeitgerechte und formal richtige Erfassung, Verarbeitung und Wiedergabe der eingegebenen Geschäftsvorfälle.', 'GoBD-24-JournalFunctionFullCapture', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 24"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 24 as a requirement that the journal function must ensure complete, timely, and formally correct recording, processing, and reproduction of business transactions.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:41:27.216514+00:00', '2026-01-16T19:41:27.216514+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('9f15c0fd-9a62-44a1-9322-b1e75b61937f', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Die Journalfunktion ist nur erfüllt, wenn die gespeicherten Aufzeichnungen gegen Veränderung oder Löschung geschützt sind.', 'GoBD-24-JournalFunctionProtection', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 24"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 24 as a requirement that the journal function is only fulfilled if stored records are protected against alteration or deletion.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:48:19.413151+00:00', '2026-01-16T19:48:19.413151+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('ea3a0453-0afd-40d7-8cd5-da342ddb1845', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Fehlerhafte Buchungen können wirksam und nachvollziehbar durch Stornierungen oder Neubuchungen geändert werden.', 'GoBD-24-ErrorCorrection', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 24"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 24 as a requirement that erroneous bookings can be corrected via cancellations or new entries, ensuring traceability.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:49:08.178947+00:00', '2026-01-16T19:49:08.178947+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('60a7e884-63ad-43b7-9940-ccc872249743', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Für die Erfüllung der Journalfunktion und Kontenfunktion sind bei der Buchung insbesondere folgende Angaben zu erfassen: eindeutige Belegnummer, Buchungsbetrag, Währungsangabe, Erläuterung des Geschäftsvorfalls, Belegdatum, Buchungsdatum, Erfassungsdatum, Autorisierung, Buchungsperiode, Umsatzsteuersatz, Steuerschlüssel, Umsatzsteuerbetrag, Umsatzsteuerkonto, Steuernummer, Konto und Gegenkonto, Buchungsschlüssel, Soll- und Haben-Betrag, eindeutige Identifikationsnummer des Geschäftsvorfalls.', 'GoBD-24-MandatoryJournalFields', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 24"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 24 as a requirement specifying the mandatory data fields for journal and ledger entries to ensure completeness and traceability.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:49:35.846581+00:00', '2026-01-16T19:49:35.846581+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('8ea51d27-4085-4e05-8883-717b039a4109', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Die im Journal erfassten Geschäftsvorfälle müssen die oben genannten Pflichtangaben enthalten, um die Nachvollziehbarkeit und Prüfbarkeit sicherzustellen.', 'GoBD-25-JournalMandatoryFields', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 25"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 25 as a requirement that journal entries must contain the mandatory fields to ensure traceability and auditability.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:50:41.720225+00:00', '2026-01-16T19:50:41.720225+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('2d6ddaa7-9e16-4277-9559-cba938f6be6c', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Der Steuerpflichtige muss ein Internes Kontrollsystem (IKS) einrichten, ausüben und protokollieren, das u. a. Zugangs- und Zugriffsberechtigungskontrollen, Funktionstrennungen, Erfassungskontrollen, Abstimmungskontrollen, Verarbeitungskontrollen und Schutzmaßnahmen gegen Verfälschung von Programmen, Daten und Dokumenten umfasst.', 'GoBD-26-InternalControlSystem', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 26"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 26 as a requirement to establish, operate, and document an internal control system covering access controls, segregation of duties, and data integrity measures.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:51:39.483147+00:00', '2026-01-16T19:51:39.483147+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('c9d3aa80-e62d-43d9-a839-8ca09332e84b', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Der Steuerpflichtige hat sein DV-System gegen Verlust, Unauffindbarkeit, Vernichtung, Diebstahl sowie gegen unberechtigte Eingaben und Veränderungen zu sichern und zu schützen.', 'GoBD-27-DataSecurity', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 27"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 27 as a requirement that the taxpayer must secure and protect the IT system against loss, unavailability, destruction, theft, and unauthorized inputs or changes.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:55:45.676950+00:00', '2026-01-16T19:55:45.676950+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('e0f11106-138f-4739-bfae-2bb276f1867c', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Werden die Daten, Datensätze, elektronischen Dokumente und Unterlagen nicht ausreichend geschützt, ist die Buchführung formell nicht mehr ordnungsmäßig.', 'GoBD-27-BookkeepingImproperIfDataNotProtected', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 27"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 27 as a requirement that if data and documents are not sufficiently protected, the bookkeeping is not formally compliant.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T19:58:26.386263+00:00', '2026-01-16T19:58:26.386263+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('8275c84f-d7de-4e44-9c28-12c1c3588d09', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Maschinell auswertbare Daten, die der Aufzeichnungs- und Aufbewahrungspflicht unterliegen, müssen in einem unkomprimierten, maschinenlesbaren Format gespeichert werden, um automatisierte Verarbeitung und Analyse ohne Informationsverlust zu ermöglichen.', 'GoBD-31-ApplicableRecordTypes', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 31"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 31 as a requirement that machine-readable data must be available for electronic primary records, journal data, and structured text files.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T20:52:21.668844+00:00', '2026-01-16T20:52:21.668844+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('2cad46cf-d3b0-4a73-a9b6-59d16c2e0a92', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Mathematisch‑technische Auswertung muss für alle aufzeichnungs‑ und aufbewahrungspflichtigen Daten, Datensätze, elektronischen Dokumente und Unterlagen automatisiert (DV‑gestützt) interpretiert, dargestellt, verarbeitet und für weitere Datenbank‑Anwendungen und Prüfsoftware nutzbar gemacht werden, ohne Informationsverlust.', 'GoBD-31-MathTechEvaluation', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 31"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 31 as a requirement for mathematical-technical evaluation of all record- and retention-relevant data in an automated, lossless manner.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T20:55:05.216190+00:00', '2026-01-16T20:55:05.216190+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('04ece41e-8c94-47cc-9f5b-2cf560fba1c6', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Mathematisch‑technische Auswertungen sind möglich für elektronische Grund(buch)aufzeichnungen (z. B. Kassendaten, Warenwirtschaftssysteme, Inventurlisten), Journaldaten aus Finanz‑ und Lohnbuchhaltung sowie strukturierte Text‑ und Tabellendateien (z. B. Reisekostenabrechnung, Überstundennachweise).', 'GoBD-31-MathTechEvaluationScope', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 31"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 31 as a requirement to support mathematical‑technical evaluation of various electronic records and structured data files.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T20:56:36.300899+00:00', '2026-01-16T20:56:36.300899+00:00', NULL, '[]');
INSERT INTO requirements ("id", "job_id", "requirement_id", "text", "name", "type", "priority", "source_document", "source_location", "gobd_relevant", "gdpr_relevant", "citations", "mentioned_objects", "mentioned_messages", "reasoning", "research_notes", "confidence", "neo4j_id", "validation_result", "rejection_reason", "status", "retry_count", "last_error", "created_at", "updated_at", "validated_at", "tags") VALUES ('beee4ae3-c51f-463a-90df-24e4d3b8fcc6', 'bad7b675-da31-439f-a32e-e9505c8ab308', NULL, 'Alle für die maschinelle Auswertung notwendigen Strukturinformationen (Dateiherkunft, Dateistruktur, Datenfelder, Zeichensatztabellen) müssen in maschinell auswertbarer, unverdichteter Form aufbewahrt werden, ebenso interne und externe Verknüpfungen.', 'GoBD-31-StructureInfoPreservation', 'functional', 'high', 'documents/GoBD.pdf', '{"section": "Page 31"}', TRUE, FALSE, '[]', '[]', '[]', 'Extracted from GoBD.pdf page 31 as a requirement to preserve all structural information needed for machine evaluation in an uncompressed, machine-readable format.', NULL, 0.9, NULL, NULL, NULL, 'pending', 0, NULL, '2026-01-16T20:57:39.972074+00:00', '2026-01-16T20:57:39.972074+00:00', NULL, '[]');

-- 64 requirement(s) exported

-- ============================================================================
-- SOURCES (citation_tool)
-- ============================================================================

INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (1, 'document', '/home/ghost/Repositories/Uni-Projekt-Graph-RAG/workspace/job_bad7b675-da31-439f-a32e-e9505c8ab308/documents/GoBD.pdf', 'GoBD.pdf', NULL, '--- Page 1 ---
 
Postanschrift Berlin: Bundesministeriu m der Finanzen, 11016 Berlin  
www.bundesfinanzministerium.de
 
 
 
 
POSTANSCHRIFT
Bundesministerium der Finanzen, 11016 Berlin 
 
Nur per E-Mail 
Oberste Finanzbehörden 
der Länder 
- bp@finmail.de - 
HAUSANSCHRIFT Wilhelmstraße 97 
10117 Berlin 
 
TEL +49 (0) 30 18 682-0 
 
 
 
E-MAIL poststelle@bmf.bund.de 
 
DATUM 28. November 2019 
 
 
 
BETREFF Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, 
Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff 
(GoBD) 
GZ IV A 4 - S 0316/19/10003 :001 
DOK 2019/0962810 
(bei Antwort bitte GZ und DOK angeben) 
 
Unter Bezugnahme auf das Ergebnis der Erörterungen mit den obersten Finanzbehörden der 
Länder gilt für die Anwendung dieser Grundsätze Folgendes: 
 
 


--- Page 2 ---
 
Seite 2
Inhalt 
1. 
ALLGEMEINES .......................................................................................................................................... 4 
1.1 
NUTZBARMACHUNG AUßERSTEUERLICHER BUCHFÜHRUNGS- UND AUFZEICHNUNGSPFLICHTEN FÜR DAS STEUERRECHT 4 
1.2 
STEUERLICHE BUCHFÜHRUNGS- UND AUFZEICHNUNGSPFLICHTEN ....................................................................... 4 
1.3 
AUFBEWAHRUNG VON UNTERLAGEN ZU GESCHÄFTSVORFÄLLEN UND VON SOLCHEN UNTERLAGEN, DIE ZUM 
VERSTÄNDNIS UND ZUR ÜBERPRÜFUNG DER FÜR DIE BESTEUERUNG GESETZLICH VORGESCHRIEBENEN 
AUFZEICHNUNGEN VON BEDEUTUNG SIND ...................................................................................................... 4 
1.4 
ORDNUNGSVORSCHRIFTEN ........................................................................................................................... 5 
1.5 
FÜHRUNG VON BÜCHERN UND SONST ERFORDERLICHEN AUFZEICHNUNGEN AUF DATENTRÄGERN ............................ 5 
1.6 
BEWEISKRAFT VON BUCHFÜHRUNG UND AUFZEICHNUNGEN, DARSTELLUNG VON BEANSTANDUNGEN DURCH DIE 
FINANZVERWALTUNG .................................................................................................................................. 6 
1.7 
AUFZEICHNUNGEN ...................................................................................................................................... 6 
1.8 
BÜCHER .................................................................................................................................................... 7 
1.9 
GESCHÄFTSVORFÄLLE .................................................................................................................................. 7 
1.10 
GRUNDSÄTZE ORDNUNGSMÄßIGER BUCHFÜHRUNG (GOB) ............................................................................... 7 
1.11 
DATENVERARBEITUNGSSYSTEM; HAUPT-, VOR- UND NEBENSYSTEME ................................................................. 8 
2. 
VERANTWORTLICHKEIT ........................................................................................................................... 8 
3. 
ALLGEMEINE ANFORDERUNGEN.............................................................................................................. 8 
3.1 
GRUNDSATZ DER NACHVOLLZIEHBARKEIT UND NACHPRÜFBARKEIT (§ 145 ABSATZ 1 AO, § 238 ABSATZ 1 SATZ 2 
UND SATZ 3 HGB) ................................................................................................................................... 10 
3.2 
GRUNDSÄTZE DER WAHRHEIT, KLARHEIT UND FORTLAUFENDEN AUFZEICHNUNG ................................................. 10 
3.2.1 
Vollständigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) ............................................................. 10 
3.2.2 
Richtigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) .................................................................... 12 
3.2.3 
Zeitgerechte Buchungen und Aufzeichnungen (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB)............ 12 
3.2.4 
Ordnung (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) ....................................................................... 14 
3.2.5 
Unveränderbarkeit (§ 146 Absatz 4 AO, § 239 Absatz 3 HGB) ....................................................... 15 
4. 
BELEGWESEN (BELEGFUNKTION) ............................................................................................................16 
4.1 
BELEGSICHERUNG ..................................................................................................................................... 17 
4.2 
ZUORDNUNG ZWISCHEN BELEG UND GRUND(BUCH)AUFZEICHNUNG ODER BUCHUNG .......................................... 17 
4.3 
ERFASSUNGSGERECHTE AUFBEREITUNG DER BUCHUNGSBELEGE ........................................................................ 18 
4.4 
BESONDERHEITEN ..................................................................................................................................... 21 
5. 
 AUFZEICHNUNG DER GESCHÄFTSVORFÄLLE IN ZEITLICHER REIHENFOLGE UND IN SACHLICHER 
ORDNUNG (GRUND(BUCH)AUFZEICHNUNGEN, JOURNAL- UND KONTENFUNKTION) .............................21 
5.1 
ERFASSUNG IN GRUND(BUCH)AUFZEICHNUNGEN ........................................................................................... 22 
5.2 
DIGITALE GRUND(BUCH)AUFZEICHNUNGEN................................................................................................... 22 
5.3 
VERBUCHUNG IM JOURNAL (JOURNALFUNKTION) .......................................................................................... 23 
5.4 
AUFZEICHNUNG DER GESCHÄFTSVORFÄLLE IN SACHLICHER ORDNUNG (HAUPTBUCH) ........................................... 24 
6. 
INTERNES KONTROLLSYSTEM (IKS) .........................................................................................................25 
7. 
DATENSICHERHEIT ..................................................................................................................................26 
8. 
UNVERÄNDERBARKEIT, PROTOKOLLIERUNG VON ÄNDERUNGEN ..........................................................26 


--- Page 3 ---
 
Seite 3
9.  
     AUFBEWAHRUNG ..............................................................................................................................28 
9.1 
MASCHINELLE AUSWERTBARKEIT (§ 147 ABSATZ 2 NUMMER 2 AO) ............................................................... 30 
9.2 
ELEKTRONISCHE AUFBEWAHRUNG ............................................................................................................... 31 
9.3 
BILDLICHE ERFASSUNG VON PAPIERDOKUMENTEN ......................................................................................... 33 
9.4 
AUSLAGERUNG VON DATEN AUS DEM PRODUKTIVSYSTEM UND SYSTEMWECHSEL ................................................ 34 
10. 
NACHVOLLZIEHBARKEIT UND NACHPRÜFBARKEIT .............................................................................35 
10.1 
VERFAHRENSDOKUMENTATION ................................................................................................................... 36 
10.2 
LESBARMACHUNG VON ELEKTRONISCHEN UNTERLAGEN .................................................................................. 37 
11. 
DATENZUGRIFF ...................................................................................................................................37 
11.1 
UMFANG UND AUSÜBUNG DES RECHTS AUF DATENZUGRIFF NACH § 147 ABSATZ 6 AO ...................................... 38 
11.2 
UMFANG DER MITWIRKUNGSPFLICHT NACH §§ 147 ABSATZ 6 UND 200 ABSATZ 1 SATZ 2 AO ............................ 40 
12. 
ZERTIFIZIERUNG UND SOFTWARE-TESTATE ........................................................................................42 
13. 
ANWENDUNGSREGELUNG .................................................................................................................42 
 
 
 


--- Page 4 ---
 
Seite 4
1. Allgemeines 
1 
 Die betrieblichen Abläufe in den Unternehmen werden ganz oder teilweise unter Ein-
satz von Informations- und Kommunikations-Technik abgebildet. 
2 
Auch die nach außersteuerlichen oder steuerlichen Vorschriften zu führenden Bücher 
und sonst erforderlichen Aufzeichnungen werden in den Unternehmen zunehmend in 
elektronischer Form geführt (z. B. als Datensätze). Darüber hinaus werden in den 
Unternehmen zunehmend die aufbewahrungspflichtigen Unterlagen in elektronischer 
Form (z. B. als elektronische Dokumente) aufbewahrt. 
1.1 Nutzbarmachung außersteuerlicher Buchführungs- und Aufzeichnungs-
pflichten für das Steuerrecht 
3 
Nach § 140 AO sind die außersteuerlichen Buchführungs- und Aufzeichnungspflich-
ten, die für die Besteuerung von Bedeutung sind, auch für das Steuerrecht zu erfüllen. 
Außersteuerliche Buchführungs- und Aufzeichnungspflichten ergeben sich insbeson-
dere aus den Vorschriften der §§ 238 ff. HGB und aus den dort bezeichneten handels-
rechtlichen Grundsätzen ordnungsmäßiger Buchführung (GoB). Für einzelne Rechts-
formen ergeben sich flankierende Aufzeichnungspflichten z. B. aus §§ 91 ff. Aktien-
gesetz, §§ 41 ff. GmbH-Gesetz oder § 33 Genossenschaftsgesetz. Des Weiteren sind 
zahlreiche gewerberechtliche oder branchenspezifische Aufzeichnungsvorschriften 
vorhanden, die gem. § 140 AO im konkreten Einzelfall für die Besteuerung von 
Bedeutung sind, wie z. B. Apothekenbetriebsordnung, Eichordnung, Fahrlehrergesetz, 
Gewerbeordnung, § 26 Kreditwesengesetz oder § 55 Versicherungsaufsichtsgesetz.  
1.2 Steuerliche Buchführungs- und Aufzeichnungspflichten 
4 
 Steuerliche Buchführungs- und Aufzeichnungspflichten ergeben sich sowohl aus der 
Abgabenordnung (z. B. §§ 90 Absatz 3, 141 bis 144 AO) als auch aus Einzelsteuer-
gesetzen (z. B. § 22 UStG, § 4 Absatz 3 Satz 5, § 4 Absatz 4a Satz 6, § 4 Absatz 7 und 
§ 41 EStG). 
1.3 Aufbewahrung von Unterlagen zu Geschäftsvorfällen und von solchen 
Unterlagen, die zum Verständnis und zur Überprüfung der für die Besteue-
rung gesetzlich vorgeschriebenen Aufzeichnungen von Bedeutung sind 
5 
Neben den außersteuerlichen und steuerlichen Büchern, Aufzeichnungen und Unter-
lagen zu Geschäftsvorfällen sind alle Unterlagen aufzubewahren, die zum Verständnis 
und zur Überprüfung der für die Besteuerung gesetzlich vorgeschriebenen Aufzeich-
nungen im Einzelfall von Bedeutung sind (vgl. BFH-Urteil vom 24. Juni 2009, 


--- Page 5 ---
 
Seite 5
BStBl II 2010 S. 452).  
 
Dazu zählen neben Unterlagen in Papierform auch alle Unterlagen in Form von Daten, 
Datensätzen und elektronischen Dokumenten, die dokumentieren, dass die Ordnungs-
vorschriften umgesetzt und deren Einhaltung überwacht wurde. Nicht aufbewahrungs-
pflichtig sind z. B. reine Entwürfe von Handels- oder Geschäftsbriefen, sofern diese 
nicht tatsächlich abgesandt wurden. 
 
Beispiel 1: 
Dienen Kostenstellen der Bewertung von Wirtschaftsgütern, von Rückstellungen oder 
als Grundlage für die Bemessung von Verrechnungspreisen sind diese Aufzeichnun-
gen aufzubewahren, soweit sie zur Erläuterung steuerlicher Sachverhalte benötigt 
werden. 
 
6 
Form, Umfang und Inhalt dieser im Sinne der Rzn. 3 bis 5 nach außersteuerlichen und 
steuerlichen Rechtsnormen aufzeichnungs- und aufbewahrungspflichtigen Unterlagen 
(Daten, Datensätze sowie Dokumente in elektronischer oder Papierform) und der zu 
ihrem Verständnis erforderlichen Unterlagen werden durch den Steuerpflichtigen 
bestimmt. Eine abschließende Definition der aufzeichnungs- und aufbewahrungs-
pflichtigen Aufzeichnungen und Unterlagen ist nicht Gegenstand der nachfolgenden 
Ausführungen. Die Finanzverwaltung kann diese Unterlagen nicht abstrakt im Vorfeld 
für alle Unternehmen abschließend definieren, weil die betrieblichen Abläufe, die auf-
zeichnungs- und aufbewahrungspflichtigen Aufzeichnungen und Unterlagen sowie die 
eingesetzten Buchführungs- und Aufzeichnungssysteme in den Unternehmen zu unter-
schiedlich sind. 
1.4 Ordnungsvorschriften 
7 
Die Ordnungsvorschriften der §§ 145 bis 147 AO gelten für die vorbezeichneten 
Bücher und sonst erforderlichen Aufzeichnungen und der zu ihrem Verständnis 
erforderlichen Unterlagen (vgl. Rzn. 3 bis 5; siehe auch Rzn. 23, 25 und 28). 
1.5 Führung von Büchern und sonst erforderlichen Aufzeichnungen auf 
Datenträgern 
8 
 Bücher und die sonst erforderlichen Aufzeichnungen können nach § 146 Absatz 5 AO 
auch auf Datenträgern geführt werden, soweit diese Form der Buchführung einschließ-
lich des dabei angewandten Verfahrens den GoB entspricht (siehe unter 1.4.). Bei Auf-
zeichnungen, die allein nach den Steuergesetzen vorzunehmen sind, bestimmt sich die 
Zulässigkeit des angewendeten Verfahrens nach dem Zweck, den die Aufzeichnungen 
für die Besteuerung erfüllen sollen (§ 145 Absatz 2 AO; § 146 Absatz 5 Satz 1 2. HS 


--- Page 6 ---
 
Seite 6
AO). Unter diesen Voraussetzungen sind auch Aufzeichnungen auf Datenträgern 
zulässig. 
9 
Somit sind alle Unternehmensbereiche betroffen, in denen betriebliche Abläufe durch 
DV-gestützte Verfahren abgebildet werden und ein Datenverarbeitungssystem (DV-
System, siehe auch Rz. 20) für die Erfüllung der in den Rzn. 3 bis 5 bezeichneten 
außersteuerlichen oder steuerlichen Buchführungs-, Aufzeichnungs- und Aufbewah-
rungspflichten verwendet wird (siehe auch unter 11.1 zum Datenzugriffsrecht). 
10 
Technische Vorgaben oder Standards (z. B. zu Archivierungsmedien oder Kryptogra-
fieverfahren) können angesichts der rasch fortschreitenden Entwicklung und der eben-
falls notwendigen Betrachtung des organisatorischen Umfelds nicht festgelegt werden. 
Im Zweifel ist über einen Analogieschluss festzustellen, ob die Ordnungsvorschriften 
eingehalten wurden, z. B. bei einem Vergleich zwischen handschriftlich geführten 
Handelsbüchern und Unterlagen in Papierform, die in einem verschlossenen Schrank 
aufbewahrt werden, einerseits und elektronischen Handelsbüchern und Unterlagen, die 
mit einem elektronischen Zugriffsschutz gespeichert werden, andererseits. 
1.6 Beweiskraft von Buchführung und Aufzeichnungen, Darstellung von 
Beanstandungen durch die Finanzverwaltung 
11 
Nach § 158 AO sind die Buchführung und die Aufzeichnungen des Steuerpflichtigen, 
die den Vorschriften der §§ 140 bis 148 AO entsprechen, der Besteuerung zugrunde zu 
legen, soweit nach den Umständen des Einzelfalls kein Anlass besteht, ihre sachliche 
Richtigkeit zu beanstanden. Werden Buchführung oder Aufzeichnungen des Steuer-
pflichtigen im Einzelfall durch die Finanzverwaltung beanstandet, so ist durch die 
Finanzverwaltung der Grund der Beanstandung in geeigneter Form darzustellen. 
1.7 Aufzeichnungen 
12 
Aufzeichnungen sind alle dauerhaft verkörperten Erklärungen über Geschäftsvorfälle 
in Schriftform oder auf Medien mit Schriftersatzfunktion (z. B. auf Datenträgern).  
Der Begriff der Aufzeichnungen umfasst Darstellungen in Worten, Zahlen, Symbolen 
und Grafiken.  
13 
Werden Aufzeichnungen nach verschiedenen Rechtsnormen in einer Aufzeichnung 
zusammengefasst (z. B. nach §§ 238 ff. HGB und nach § 22 UStG), müssen die 
zusammengefassten Aufzeichnungen den unterschiedlichen Zwecken genügen. 
Erfordern verschiedene Rechtsnormen gleichartige Aufzeichnungen, so ist eine 
mehrfache Aufzeichnung für jede Rechtsnorm nicht erforderlich. 


--- Page 7 ---
 
Seite 7
1.8 Bücher 
14 
Der Begriff ist funktional unter Anknüpfung an die handelsrechtliche Bedeutung zu 
verstehen. Die äußere Gestalt (gebundenes Buch, Loseblattsammlung oder 
Datenträger) ist unerheblich.  
15 
Der Kaufmann ist verpflichtet, in den Büchern seine Handelsgeschäfte und die Lage 
des Vermögens ersichtlich zu machen (§ 238 Absatz 1 Satz 1 HGB). Der Begriff 
Bücher umfasst sowohl die Handelsbücher der Kaufleute (§§ 238 ff. HGB) als auch 
die diesen entsprechenden Aufzeichnungen von Geschäftsvorfällen der Nichtkauf-
leute. Bei Kleinstunternehmen, die ihren Gewinn durch Einnahmen-Überschussrech-
nung ermitteln (bis 17.500 Euro Jahresumsatz), ist die Erfüllung der Anforderungen an 
die Aufzeichnungen nach den GoBD regelmäßig auch mit Blick auf die Unterneh-
mensgröße zu bewerten. 
1.9 Geschäftsvorfälle 
16 
Geschäftsvorfälle sind alle rechtlichen und wirtschaftlichen Vorgänge, die innerhalb 
eines bestimmten Zeitabschnitts den Gewinn bzw. Verlust oder die Vermögenszusam-
mensetzung in einem Unternehmen dokumentieren oder beeinflussen bzw. verändern 
(z. B. zu einer Veränderung des Anlage- und Umlaufvermögens sowie des Eigen- und 
Fremdkapitals führen). 
1.10 
Grundsätze ordnungsmäßiger Buchführung (GoB) 
17 
Die GoB sind ein unbestimmter Rechtsbegriff, der insbesondere durch Rechtsnormen 
und Rechtsprechung geprägt ist und von der Rechtsprechung und Verwaltung jeweils 
im Einzelnen auszulegen und anzuwenden ist (BFH-Urteil vom 12. Mai 1966, BStBl III 
S. 371; BVerfG-Beschluss vom 10. Oktober 1961, 2 BvL 1/59, BVerfGE 13 S. 153). 
 
18 
Die GoB können sich durch gutachterliche Stellungnahmen, Handelsbrauch, ständige 
Übung, Gewohnheitsrecht, organisatorische und technische Änderungen weiterent-
wickeln und sind einem Wandel unterworfen. 
 
19 
Die GoB enthalten sowohl formelle als auch materielle Anforderungen an eine Buch-
führung. Die formellen Anforderungen ergeben sich insbesondere aus den §§ 238 ff. 
HGB für Kaufleute und aus den §§ 145 bis 147 AO für Buchführungs- und Aufzeich-
nungspflichtige (siehe unter 3.). Materiell ordnungsmäßig sind Bücher und Aufzeich-
nungen, wenn die Geschäftsvorfälle einzeln, nachvollziehbar, vollständig, richtig, zeit-
gerecht und geordnet in ihrer Auswirkung erfasst und anschließend gebucht bzw. ver-
arbeitet sind (vgl. § 239 Absatz 2 HGB, § 145 AO, § 146 Absatz 1 AO). Siehe Rz. 11 
zur Beweiskraft von Buchführung und Aufzeichnungen. 
 


--- Page 8 ---
 
Seite 8
1.11 
Datenverarbeitungssystem; Haupt-, Vor- und Nebensysteme 
20 
Unter DV-System wird die im Unternehmen oder für Unternehmenszwecke zur elek-
tronischen Datenverarbeitung eingesetzte Hard- und Software verstanden, mit denen 
Daten und Dokumente im Sinne der Rzn. 3 bis 5 erfasst, erzeugt, empfangen, über-
nommen, verarbeitet, gespeichert oder übermittelt werden. Dazu gehören das Haupt-
system sowie Vor- und Nebensysteme (z. B. Finanzbuchführungssystem, Anlagen-
buchhaltung, Lohnbuchhaltungssystem, Kassensystem, Warenwirtschaftssystem, 
Zahlungsverkehrssystem, Taxameter, Geldspielgeräte, elektronische Waagen, 
Materialwirtschaft, Fakturierung, Zeiterfassung, Archivsystem, Dokumenten-
Management-System) einschließlich der Schnittstellen zwischen den Systemen. Auf 
die Bezeichnung des DV-Systems oder auf dessen Größe (z. B. Einsatz von Einzel-
geräten oder von Netzwerken) kommt es dabei nicht an. Ebenfalls kommt es nicht 
darauf an, ob die betreffenden DV-Systeme vom Steuerpflichtigen als eigene 
Hardware bzw. Software erworben und genutzt oder in einer Cloud bzw. als eine 
Kombination dieser Systeme betrieben werden. 
2. Verantwortlichkeit 
21 
Für die Ordnungsmäßigkeit elektronischer Bücher und sonst erforderlicher elektroni-
scher Aufzeichnungen im Sinne der Rzn. 3 bis 5, einschließlich der eingesetzten 
Verfahren, ist allein der Steuerpflichtige verantwortlich. Dies gilt auch bei einer 
teilweisen oder vollständigen organisatorischen und technischen Auslagerung von 
Buchführungs- und Aufzeichnungsaufgaben auf Dritte (z. B. Steuerberater oder 
Rechenzentrum).  
3. Allgemeine Anforderungen 
22 
Die Ordnungsmäßigkeit elektronischer Bücher und sonst erforderlicher elektronischer 
Aufzeichnungen im Sinne der Rzn. 3 bis 5 ist nach den gleichen Prinzipien zu beur-
teilen wie die Ordnungsmäßigkeit bei manuell erstellten Büchern oder Aufzeichnun-
gen. 
23 
Das Erfordernis der Ordnungsmäßigkeit erstreckt sich - neben den elektronischen 
Büchern und sonst erforderlichen Aufzeichnungen - auch auf die damit in Zusammen-
hang stehenden Verfahren und Bereiche des DV-Systems (siehe unter 1.11), da die 
Grundlage für die Ordnungsmäßigkeit elektronischer Bücher und sonst erforderlicher 
Aufzeichnungen bereits bei der Entwicklung und Freigabe von Haupt-, Vor- und 
Nebensystemen einschließlich des dabei angewandten DV-gestützten Verfahrens 
gelegt wird. Die Ordnungsmäßigkeit muss bei der Einrichtung und unternehmens-
spezifischen Anpassung des DV-Systems bzw. der DV-gestützten Verfahren im 


--- Page 9 ---
 
Seite 9
konkreten Unternehmensumfeld und für die Dauer der Aufbewahrungsfrist erhalten 
bleiben. 
24 
Die Anforderungen an die Ordnungsmäßigkeit ergeben sich aus: 
• außersteuerlichen Rechtsnormen (z. B. den handelsrechtlichen GoB gem. §§ 238, 
239, 257, 261 HGB), die gem. § 140 AO für das Steuerrecht nutzbar gemacht 
werden können, wenn sie für die Besteuerung von Bedeutung sind, und 
• steuerlichen Ordnungsvorschriften (insbesondere gem. §§ 145 bis 147 AO). 
25 
Die allgemeinen Ordnungsvorschriften in den §§ 145 bis 147 AO gelten nicht nur für 
Buchführungs- und Aufzeichnungspflichten nach § 140 AO und nach den §§ 141 
bis 144 AO. Insbesondere § 145 Absatz 2 AO betrifft alle zu Besteuerungszwecken 
gesetzlich geforderten Aufzeichnungen, also auch solche, zu denen der Steuer-
pflichtige aufgrund anderer Steuergesetze verpflichtet ist, wie z. B. nach § 4 Absatz 3 
Satz 5, Absatz 7 EStG und nach § 22 UStG (BFH-Urteil vom 24. Juni 2009, 
BStBl II 2010 S. 452). 
 
26 
Demnach sind bei der Führung von Büchern in elektronischer oder in Papierform und 
sonst erforderlicher Aufzeichnungen in elektronischer oder in Papierform im Sinne der 
Rzn. 3 bis 5 die folgenden Anforderungen zu beachten: 
• Grundsatz der Nachvollziehbarkeit und Nachprüfbarkeit (siehe unter 3.1), 
• Grundsätze der Wahrheit, Klarheit und fortlaufenden Aufzeichnung (siehe 
unter 3.2): 
 Vollständigkeit (siehe unter 3.2.1), 
 Einzelaufzeichnungspflicht (siehe unter 3.2.1), 
 Richtigkeit (siehe unter 3.2.2), 
 zeitgerechte Buchungen und Aufzeichnungen (siehe unter 3.2.3), 
 Ordnung (siehe unter 3.2.4), 
 Unveränderbarkeit (siehe unter 3.2.5). 
 
27 
Diese Grundsätze müssen während der Dauer der Aufbewahrungsfrist nachweisbar 
erfüllt werden und erhalten bleiben. 
28 
Nach § 146 Absatz 6 AO gelten die Ordnungsvorschriften auch dann, wenn der Unter-
nehmer elektronische Bücher und Aufzeichnungen führt, die für die Besteuerung von 
Bedeutung sind, ohne hierzu verpflichtet zu sein. 
 
29 
Der Grundsatz der Wirtschaftlichkeit rechtfertigt es nicht, dass Grundprinzipien der 
Ordnungsmäßigkeit verletzt und die Zwecke der Buchführung erheblich gefährdet 
werden. Die zur Vermeidung einer solchen Gefährdung erforderlichen Kosten muss 
der Steuerpflichtige genauso in Kauf nehmen wie alle anderen Aufwendungen, die die 
Art seines Betriebes mit sich bringt (BFH-Urteil vom 26. März 1968, BStBl II S. 527). 


--- Page 10 ---
 
Seite 10
3.1 Grundsatz der Nachvollziehbarkeit und Nachprüfbarkeit (§ 145 Absatz 1 
AO, § 238 Absatz 1 Satz 2 und Satz 3 HGB) 
30 
Die Verarbeitung der einzelnen Geschäftsvorfälle sowie das dabei angewandte Buch-
führungs- oder Aufzeichnungsverfahren müssen nachvollziehbar sein. Die Buchungen 
und die sonst erforderlichen Aufzeichnungen müssen durch einen Beleg nachgewiesen 
sein oder nachgewiesen werden können (Belegprinzip, siehe auch unter 4.).  
31 
Aufzeichnungen sind so vorzunehmen, dass der Zweck, den sie für die Besteuerung 
erfüllen sollen, erreicht wird. Damit gelten die nachfolgenden Anforderungen der 
progressiven und retrograden Prüfbarkeit - soweit anwendbar - sinngemäß. 
32 
Die Buchführung muss so beschaffen sein, dass sie einem sachverständigen Dritten 
innerhalb angemessener Zeit einen Überblick über die Geschäftsvorfälle und über die 
Lage des Unternehmens vermitteln kann. Die einzelnen Geschäftsvorfälle müssen sich 
in ihrer Entstehung und Abwicklung lückenlos verfolgen lassen (progressive und 
retrograde Prüfbarkeit). 
 
33 
Die progressive Prüfung beginnt beim Beleg, geht über die Grund(buch)aufzeich-
nungen und Journale zu den Konten, danach zur Bilanz mit Gewinn- und Verlust-
rechnung und schließlich zur Steueranmeldung bzw. Steuererklärung. Die retrograde 
Prüfung verläuft umgekehrt. Die progressive und retrograde Prüfung muss für die 
gesamte Dauer der Aufbewahrungsfrist und in jedem Verfahrensschritt möglich sein.  
34 
Die Nachprüfbarkeit der Bücher und sonst erforderlichen Aufzeichnungen erfordert 
eine aussagekräftige und vollständige Verfahrensdokumentation (siehe unter 10.1), die 
sowohl die aktuellen als auch die historischen Verfahrensinhalte für die Dauer der 
Aufbewahrungsfrist nachweist und den in der Praxis eingesetzten Versionen des DV-
Systems entspricht. 
 
35 
Die Nachvollziehbarkeit und Nachprüfbarkeit muss für die Dauer der Aufbewahrungs-
frist gegeben sein. Dies gilt auch für die zum Verständnis der Buchführung oder Auf-
zeichnungen erforderliche Verfahrensdokumentation. 
3.2 Grundsätze der Wahrheit, Klarheit und fortlaufenden Aufzeichnung 
3.2.1 Vollständigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) 
36 
Die Geschäftsvorfälle sind vollzählig und lückenlos aufzuzeichnen (Grundsatz der 
Einzelaufzeichnungspflicht; vgl. AEAO zu § 146 AO Nr. 2.1). Eine vollzählige und 
lückenlose Aufzeichnung von Geschäftsvorfällen ist auch dann gegeben, wenn 
zulässigerweise nicht alle Datenfelder eines Datensatzes gefüllt werden.  
37 
Die GoB erfordern in der Regel die Aufzeichnung jedes Geschäftsvorfalls - also auch 
jeder Betriebseinnahme und Betriebsausgabe, jeder Einlage und Entnahme - in einem 


--- Page 11 ---
 
Seite 11
Umfang, der eine Überprüfung seiner Grundlagen, seines Inhalts und seiner Bedeu-
tung für den Betrieb ermöglicht. Das bedeutet nicht nur die Aufzeichnung der in Geld 
bestehenden Gegenleistung, sondern auch des Inhalts des Geschäfts und des Namens 
des Vertragspartners (BFH-Urteil vom 12. Mai 1966, BStBl III S. 371) - soweit 
zumutbar, mit ausreichender Bezeichnung des Geschäftsvorfalls (BFH-Urteil vom 
1. Oktober 1969, BStBl 1970 II S. 45). Branchenspezifische Mindestaufzeichnungs-
pflichten und Zumutbarkeitsgesichtspunkte sind zu berücksichtigen. 
Beispiele 2 zu branchenspezifisch entbehrlichen Aufzeichnungen und zur 
Zumutbarkeit: 
• In einem Einzelhandelsgeschäft kommt zulässigerweise eine PC-Kasse ohne Kun-
denverwaltung zum Einsatz. Die Namen der Kunden werden bei Bargeschäften 
nicht erfasst und nicht beigestellt. - Keine Beanstandung. 
• Bei einem Taxiunternehmer werden Angaben zum Kunden im Taxameter nicht 
erfasst und nicht beigestellt. - Keine Beanstandung. 
 
38 
Dies gilt auch für Bareinnahmen; der Umstand der sofortigen Bezahlung rechtfertigt 
keine Ausnahme von diesem Grundsatz (BFH-Urteil vom 26. Februar 2004, BStBl II 
S. 599). 
39 
Die Aufzeichnung jedes einzelnen Geschäftsvorfalls ist nur dann nicht zumutbar, 
wenn es technisch, betriebswirtschaftlich und praktisch unmöglich ist, die einzelnen 
Geschäftsvorfälle aufzuzeichnen (BFH-Urteil vom 12. Mai 1966, IV 472/60, BStBl III 
S. 371). Das Vorliegen dieser Voraussetzungen ist durch den Steuerpflichtigen nach-
zuweisen. 
Beim Verkauf von Waren an eine Vielzahl von nicht bekannten Personen gegen 
Barzahlung gilt die Einzelaufzeichnungspflicht nach § 146 Absatz 1 Satz 1 AO aus 
Zumutbarkeitsgrü nden nicht, wenn kein elektronisches Aufzeichnungssystem, sondern 
eine offene Ladenkasse verwendet wird (§ 146 Absatz 1 Satz 3 und 4 AO, vgl. AEAO 
zu § 146, Nr. 2.1.4). Wird hingegen ein elektronisches Aufzeichnungssystem ver-
wendet, gilt die Einzelaufzeichnungspflicht nach § 146 Absatz 1 Satz 1 AO unab-
hängig davon, ob das elektronische Aufzeichnungssystem und die digitalen Aufzeich-
nungen nach § 146a Absatz 3 AO i. V. m. der KassenSichV mit einer zertifizierten 
technischen Sicherheitseinrichtung zu schü tzen sind. Die Zumutbarkeitsü berlegungen, 
die der Ausnahmeregelung nach § 146 Absatz 1 Satz 3 AO zugrunde liegen, sind 
grundsätzlich auch auf Dienstleistungen ü bertragbar (vgl. AEAO zu § 146, Nr. 2.2.6). 
40 
Die vollständige und lückenlose Erfassung und Wiedergabe aller Geschäftsvorfälle ist 
bei DV-Systemen durch ein Zusammenspiel von technischen (einschließlich program-
mierten) und organisatorischen Kontrollen sicherzustellen (z. B. Erfassungskontrollen, 


--- Page 12 ---
 
Seite 12
Plausibilitätskontrollen bei Dateneingaben, inhaltliche Plausibilitätskontrollen, auto-
matisierte Vergabe von Datensatznummern, Lückenanalyse oder Mehrfachbelegungs-
analyse bei Belegnummern).  
41 
Ein und derselbe Geschäftsvorfall darf nicht mehrfach aufgezeichnet werden. 
Beispiel 3: 
Ein Wareneinkauf wird gewinnwirksam durch Erfassung des zeitgleichen Liefer-
scheins und später nochmals mittels Erfassung der (Sammel)Rechnung erfasst und 
verbucht. Keine mehrfache Aufzeichnung eines Geschäftsvorfalles in verschiedenen 
Systemen oder mit verschiedenen Kennungen (z. B. für Handelsbilanz, für steuerliche 
Zwecke) liegt vor, soweit keine mehrfache bilanzielle oder gewinnwirksame Auswir-
kung gegeben ist.  
42 
Zusammengefasste oder verdichtete Aufzeichnungen im Hauptbuch (Konto) sind 
zulässig, sofern sie nachvollziehbar in ihre Einzelpositionen in den Grund(buch)auf-
zeichnungen oder des Journals aufgegliedert werden können. Andernfalls ist die 
Nachvollziehbarkeit und Nachprüfbarkeit nicht gewährleistet.  
43 
Die Erfassung oder Verarbeitung von tatsächlichen Geschäftsvorfällen darf nicht 
unterdrückt werden. So ist z. B. eine Bon- oder Rechnungserteilung ohne Registrie-
rung der bar vereinnahmten Beträge (Abbruch des Vorgangs) in einem DV-System 
unzulässig. 
3.2.2 Richtigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) 
44 
Geschäftsvorfälle sind in Übereinstimmung mit den tatsächlichen Verhältnissen und 
im Einklang mit den rechtlichen Vorschriften inhaltlich zutreffend durch Belege 
abzubilden (BFH-Urteil vom 24. Juni 1997, BStBl II 1998 S. 51), der Wahrheit ent-
sprechend aufzuzeichnen und bei kontenmäßiger Abbildung zutreffend zu kontieren.  
 
3.2.3 Zeitgerechte Buchungen und Aufzeichnungen (§ 146 Absatz 1 AO, § 239 
Absatz 2 HGB) 
45 
Das Erfordernis „zeitgerecht“ zu buchen verlangt, dass ein zeitlicher Zusammenhang 
zwischen den Vorgängen und ihrer buchmäßigen Erfassung besteht (BFH-Urteil vom 
25. März 1992, BStBl II S. 1010; BFH-Urteil vom 5. März 1965, BStBl III S. 285). 
46 
Jeder Geschäftsvorfall ist zeitnah, d. h. möglichst unmittelbar nach seiner Entstehung in 
einer Grundaufzeichnung oder in einem Grundbuch zu erfassen. Nach den GoB müssen 
die Geschäftsvorfälle grundsätzlich laufend gebucht werden (Journal). Es widerspricht 
dem Wesen der kaufmännischen Buchführung, sich zunächst auf die Sammlung von 
Belegen zu beschränken und nach Ablauf einer langen Zeit auf Grund dieser Belege die 
Geschäftsvorfälle in Grundaufzeichnungen oder Grundbüchern einzutragen (vgl. BFH-


--- Page 13 ---
 
Seite 13
Urteil vom 10. Juni 1954, BStBl III S. 298). Die Funktion der Grund(buch)aufzeich-
nungen kann auf Dauer auch durch eine geordnete und übersichtliche Belegablage 
erfüllt werden (§ 239 Absatz 4 HGB; § 146 Absatz 5 AO; H 5.2 „Grundbuchaufzeich-
nungen“ EStH). 
47 
Jede nicht durch die Verhältnisse des Betriebs oder des Geschäftsvorfalls zwingend 
bedingte Zeitspanne zwischen dem Eintritt des Vorganges und seiner laufenden 
Erfassung in Grund(buch)aufzeichnungen ist bedenklich. Eine Erfassung von unbaren 
Geschäftsvorfällen innerhalb von zehn Tagen ist unbedenklich. Wegen der Forderung 
nach zeitnaher chronologischer Erfassung der Geschäftsvorfälle ist zu verhindern, dass 
die Geschäftsvorfälle buchmäßig für längere Zeit in der Schwebe gehalten werden und 
sich hierdurch die Möglichkeit eröffnet, sie später anders darzustellen, als sie richtiger-
weise darzustellen gewesen wären, oder sie ganz außer Betracht zu lassen und im 
privaten, sich in der Buchführung nicht niederschlagenden Bereich abzuwickeln.  
Bei zeitlichen Abständen zwischen der Entstehung eines Geschäftsvorfalls und seiner 
Erfassung sind daher geeignete Maßnahmen zur Sicherung der Vollständigkeit zu 
treffen. 
48 
Kasseneinnahmen und Kassenausgaben sind nach § 146 Absatz 1 Satz 2 AO täglich 
festzuhalten. 
49 
Es ist nicht zu beanstanden, wenn Waren- und Kostenrechnungen, die innerhalb von 
acht Tagen nach Rechnungseingang oder innerhalb der ihrem gewöhnlichen Durchlauf 
durch den Betrieb entsprechenden Zeit beglichen werden, kontokorrentmäßig nicht 
(z. B. Geschäftsfreundebuch, Personenkonten) erfasst werden (vgl. R 5.2 Absatz 1 
EStR).  
50 
Werden bei der Erstellung der Bücher Geschäftsvorfälle nicht laufend, sondern nur 
periodenweise gebucht bzw. den Büchern vergleichbare Aufzeichnungen der Nicht-
buchführungspflichtigen nicht laufend, sondern nur periodenweise erstellt, dann ist 
dies unter folgenden Voraussetzungen nicht zu beanstanden: 
• Die Geschäftsvorfälle werden vorher zeitnah (bare Geschäftsvorfälle täglich, 
unbare Geschäftsvorfälle innerhalb von zehn Tagen) in Grund(buch)aufzeichnun-
gen oder Grundbüchern festgehalten und durch organisatorische Vorkehrungen ist 
sichergestellt, dass die Unterlagen bis zu ihrer Erfassung nicht verloren gehen, 
z. B. durch laufende Nummerierung der eingehenden und ausgehenden Rechnun-
gen, durch Ablage in besonderen Mappen und Ordnern oder durch elektronische 
Grund(buch)aufzeichnungen in Kassensystemen, Warenwirtschaftssystemen, 
Fakturierungssystemen etc., 
• die Vollständigkeit der Geschäftsvorfälle wird im Einzelfall gewährleistet und 


--- Page 14 ---
 
Seite 14
• es wurde zeitnah eine Zuordnung (Kontierung, mindestens aber die Zuordnung 
betrieblich / privat, Ordnungskriterium für die Ablage) vorgenommen. 
51 
Jeder Geschäftsvorfall ist periodengerecht der Abrechnungsperiode zuzuordnen, in der 
er angefallen ist. Zwingend ist die Zuordnung zum jeweiligen Geschäftsjahr oder zu 
einer nach Gesetz, Satzung oder Rechnungslegungszweck vorgeschriebenen kürzeren 
Rechnungsperiode. 
 
52 
Erfolgt die Belegsicherung oder die Erfassung von Geschäftsvorfällen unmittelbar 
nach Eingang oder Entstehung mittels DV-System (elektronische Grund(buch)auf-
zeichnungen), so stellt sich die Frage der Zumutbarkeit und Praktikabilität hinsichtlich 
der zeitgerechten Erfassung/Belegsicherung und längerer Fristen nicht. Erfüllen die 
Erfassungen Belegfunktion bzw. dienen sie der Belegsicherung (auch für Vorsysteme, 
wie Kasseneinzelaufzeichnungen und Warenwirtschaftssystem), dann ist eine unproto-
kollierte Änderung nicht mehr zulässig (siehe unter 3.2.5). Bei zeitlichen Abständen 
zwischen Erfassung und Buchung, die über den Ablauf des folgenden Monats hinaus-
gehen, sind die Ordnungsmäßigkeitsanforderungen nur dann erfüllt, wenn die 
Geschäftsvorfälle vorher fortlaufend richtig und vollständig in Grund(buch)aufzeich-
nungen oder Grundbüchern festgehalten werden (vgl. Rz. 50). Zur Erfüllung der Funk-
tion der Grund(buch)aufzeichnung vgl. Rz. 46. 
3.2.4 Ordnung (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) 
53 
Der Grundsatz der Klarheit verlangt u. a. eine systematische Erfassung und übersicht-
liche, eindeutige und nachvollziehbare Buchungen.  
54 
Die geschäftlichen Unterlagen dürfen nicht planlos gesammelt und aufbewahrt wer-
den. Ansonsten würde dies mit zunehmender Zahl und Verschiedenartigkeit der 
Geschäftsvorfälle zur Unübersichtlichkeit der Buchführung führen, einen jederzeitigen 
Abschluss unangemessen erschweren und die Gefahr erhöhen, dass Unterlagen ver-
lorengehen oder später leicht aus dem Buchführungswerk entfernt werden können. 
Hieraus folgt, dass die Bücher und Aufzeichnungen nach bestimmten Ordnungsprin-
zipien geführt werden müssen und eine Sammlung und Aufbewahrung der Belege not-
wendig ist, durch die im Rahmen des Möglichen gewährleistet wird, dass die 
Geschäftsvorfälle leicht und identifizierbar feststellbar und für einen die Lage des 
Vermögens darstellenden Abschluss unverlierbar sind (BFH-Urteil vom 26. März 
1968, BStBl II S. 527).  
55 
In der Regel verstößt die nicht getrennte Verbuchung von baren und unbaren 
Geschäftsvorfällen oder von nicht steuerbaren, steuerfreien und steuerpflichtigen 
Umsätzen ohne genügende Kennzeichnung gegen die Grundsätze der Wahrheit und 
Klarheit einer kaufmännischen Buchführung. Die nicht getrennte Aufzeichnung von 
nicht steuerbaren, steuerfreien und steuerpflichtigen Umsätzen ohne genügende 


--- Page 15 ---
 
Seite 15
Kennzeichnung verstößt in der Regel gegen steuerrechtliche Anforderungen (z. B. 
§ 22 UStG). Eine kurzzeitige gemeinsame Erfassung von baren und unbaren Tages-
geschäften im Kassenbuch ist regelmäßig nicht zu beanstanden, wenn die ursprünglich 
im Kassenbuch erfassten unbaren Tagesumsätze (z. B. EC-Kartenumsätze) gesondert 
kenntlich gemacht sind und nachvollziehbar unmittelbar nachfolgend wieder aus dem 
Kassenbuch auf ein gesondertes Konto aus- bzw. umgetragen werden, soweit die 
Kassensturzfähigkeit der Kasse weiterhin gegeben ist. 
56 
Bei der doppelten Buchführung sind die Geschäftsvorfälle so zu verarbeiten, dass sie 
geordnet darstellbar sind und innerhalb angemessener Zeit ein Überblick über die Ver-
mögens- und Ertragslage gewährleistet ist.  
57 
Die Buchungen müssen einzeln und sachlich geordnet nach Konten dargestellt (Kon-
tenfunktion) und unverzüglich lesbar gemacht werden können. Damit bei Bedarf für 
einen zurückliegenden Zeitpunkt ein Zwischenstatus oder eine Bilanz mit Gewinn- 
und Verlustrechnung aufgestellt werden kann, sind die Konten nach Abschluss-
positionen zu sammeln und nach Kontensummen oder Salden fortzuschreiben 
(Hauptbuch, siehe unter 5.4). 
3.2.5 Unveränderbarkeit (§ 146 Absatz 4 AO, § 239 Absatz 3 HGB) 
58 
Eine Buchung oder eine Aufzeichnung darf nicht in einer Weise verändert werden, 
dass der ursprüngliche Inhalt nicht mehr feststellbar ist. Auch solche Veränderungen 
dürfen nicht vorgenommen werden, deren Beschaffenheit es ungewiss lässt, ob sie 
ursprünglich oder erst später gemacht worden sind (§ 146 Absatz 4 AO, § 239 
Absatz 3 HGB). 
  
59 
Veränderungen und Löschungen von und an elektronischen Buchungen oder Auf-
zeichnungen (vgl. Rzn. 3 bis 5) müssen daher so protokolliert werden, dass die 
Voraussetzungen des § 146 Absatz 4 AO bzw. § 239 Absatz 3 HGB erfüllt sind (siehe 
auch unter 8). Für elektronische Dokumente und andere elektronische Unterlagen, die 
gem. § 147 AO aufbewahrungspflichtig und nicht Buchungen oder Aufzeichnungen 
sind, gilt dies sinngemäß.  
Beispiel 4: 
Der Steuerpflichtige erstellt über ein Fakturierungssystem Ausgangsrechnungen und 
bewahrt die inhaltlichen Informationen elektronisch auf (zum Beispiel in seinem 
Fakturierungssystem). Die Lesbarmachung der abgesandten Handels- und Geschäfts-
briefe aus dem Fakturierungssystem erfolgt jeweils unter Berücksichtigung der in den 
aktuellen Stamm- und Bewegungsdaten enthaltenen Informationen. 


--- Page 16 ---
 
Seite 16
In den Stammdaten ist im Jahr 01 der Steuersatz 16 % und der Firmenname des Kun-
den A hinterlegt. Durch Umfirmierung des Kunden A zu B und Änderung des Steuer-
satzes auf 19 % werden die Stammdaten im Jahr 02 geändert. Eine Historisierung der 
Stammdaten erfolgt nicht. 
Der Steuerpflichtige ist im Jahr 02 nicht mehr in der Lage, die inhaltliche Überein-
stimmung der abgesandten Handels- und Geschäftsbriefe mit den ursprünglichen 
Inhalten bei Lesbarmachung sicher zu stellen. 
 
60 
Der Nachweis der Durchführung der in dem jeweiligen Verfahren vorgesehenen 
Kontrollen ist u. a. durch Verarbeitungsprotokolle sowie durch die Verfahrens-
dokumentation (siehe unter 6. und unter 10.1) zu erbringen. 
4. Belegwesen (Belegfunktion) 
61 
Jeder Geschäftsvorfall ist urschriftlich bzw. als Kopie der Urschrift zu belegen.  
Ist kein Fremdbeleg vorhanden, muss ein Eigenbeleg erstellt werden. Zweck der 
Belege ist es, den sicheren und klaren Nachweis über den Zusammenhang zwischen 
den Vorgängen in der Realität einerseits und dem aufgezeichneten oder gebuchten 
Inhalt in Büchern oder sonst erforderlichen Aufzeichnungen und ihre Berechtigung 
andererseits zu erbringen (Belegfunktion). Auf die Bezeichnung als „Beleg“ kommt es 
nicht an. Die Belegfunktion ist die Grundvoraussetzung für die Beweiskraft der 
Buchführung und sonst erforderlicher Aufzeichnungen. Sie gilt auch bei Einsatz eines 
DV-Systems.  
62 
Inhalt und Umfang der in den Belegen enthaltenen Informationen sind insbesondere 
von der Belegart (z. B. Aufträge, Auftragsbestätigungen, Bescheide über Steuern oder 
Gebühren, betriebliche Kontoauszüge, Gutschriften, Lieferscheine, Lohn- und 
Gehaltsabrechnungen, Barquittungen, Rechnungen, Verträge, Zahlungsbelege) und der 
eingesetzten Verfahren abhängig.  
 
63 
Empfangene oder abgesandte Handels- oder Geschäftsbriefe erhalten erst mit dem 
Kontierungsvermerk und der Verbuchung auch die Funktion eines Buchungsbelegs. 
64 
Zur Erfüllung der Belegfunktionen sind deshalb Angaben zur Kontierung, zum 
Ordnungskriterium für die Ablage und zum Buchungsdatum auf dem Papierbeleg 
erforderlich. Bei einem elektronischen Beleg kann dies auch durch die Verbindung mit 
einem Datensatz mit Angaben zur Kontierung oder durch eine elektronische Verknüp-
fung (z. B. eindeutiger Index, Barcode) erfolgen. Ein Steuerpflichtiger hat andernfalls 
durch organisatorische Maßnahmen sicherzustellen, dass die Geschäftsvorfälle auch 
ohne Angaben auf den Belegen in angemessener Zeit progressiv und retrograd nach-
prüfbar sind.  


--- Page 17 ---
 
Seite 17
 
Korrektur- bzw. Stornobuchungen müssen auf die ursprüngliche Buchung rück-
beziehbar sein. 
65 
Ein Buchungsbeleg in Papierform oder in elektronischer Form (z. B. Rechnung) kann 
einen oder mehrere Geschäftsvorfälle enthalten.  
66 
Aus der Verfahrensdokumentation (siehe unter 10.1) muss ersichtlich sein, wie die 
elektronischen Belege erfasst, empfangen, verarbeitet, ausgegeben und aufbewahrt 
(zur Aufbewahrung siehe unter 9.) werden. 
4.1 Belegsicherung 
67 
Die Belege in Papierform oder in elektronischer Form sind zeitnah, d. h. möglichst 
unmittelbar nach Eingang oder Entstehung gegen Verlust zu sichern (vgl. zur zeit-
gerechten Belegsicherung unter 3.2.3, vgl. zur Aufbewahrung unter 9.).  
68 
Bei Papierbelegen erfolgt eine Sicherung z. B. durch laufende Nummerierung der ein-
gehenden und ausgehenden Lieferscheine und Rechnungen, durch laufende Ablage in 
besonderen Mappen und Ordnern, durch zeitgerechte Erfassung in Grund(buch)auf-
zeichnungen oder durch laufende Vergabe eines Barcodes und anschließende bildliche 
Erfassung der Papierbelege im Sinne des § 147 Absatz 2 AO (siehe Rz. 130). 
69 
Bei elektronischen Belegen (z. B. Abrechnung aus Fakturierung) kann die laufende 
Nummerierung automatisch vergeben werden (z. B. durch eine eindeutige Beleg-
nummer).  
70 
Die Belegsicherung kann organisatorisch und technisch mit der Zuordnung zwischen 
Beleg und Grund(buch)aufzeichnung oder Buchung verbunden werden. 
4.2 Zuordnung zwischen Beleg und Grund(buch)aufzeichnung oder Buchung 
71 
Die Zuordnung zwischen dem einzelnen Beleg und der dazugehörigen Grund(buch)auf-
zeichnung oder Buchung kann anhand von eindeutigen Zuordnungsmerkmalen (z. B. 
Index, Paginiernummer, Dokumenten-ID) und zusätzlichen Identifikationsmerkmalen 
für die Papierablage oder für die Such- und Filtermöglichkeit bei elektronischer Beleg-
ablage gewährleistet werden. Gehören zu einer Grund(buch)aufzeichnung oder Buchung 
mehrere Belege (z. B. Rechnung verweist für Menge und Art der gelieferten Gegenstän-
de nur auf Lieferschein), bedarf es zusätzlicher Zuordnungs- und Identifikationsmerk-
male für die Verknüpfung zwischen den Belegen und der Grund(buch)aufzeichnung 
oder Buchung. 
72 
Diese Zuordnungs- und Identifizierungsmerkmale aus dem Beleg müssen bei der Auf-
zeichnung oder Verbuchung in die Bücher oder Aufzeichnungen übernommen werden, 
um eine progressive und retrograde Prüfbarkeit zu ermöglichen. 


--- Page 18 ---
 
Seite 18
73 
Die Ablage der Belege und die Zuordnung zwischen Beleg und Aufzeichnung müssen 
in angemessener Zeit nachprüfbar sein. So kann z. B. Beleg- oder Buchungsdatum, 
Kontoauszugnummer oder Name bei umfangreichem Beleganfall mangels Eindeutig-
keit in der Regel kein geeignetes Zuordnungsmerkmal für den einzelnen Geschäftsvor-
fall sein. 
74 
Beispiel 5: 
Ein Steuerpflichtiger mit ausschließlich unbaren Geschäftsvorfällen erhält nach 
Abschluss eines jeden Monats von seinem Kreditinstitut einen Kontoauszug in 
Papierform mit vielen einzelnen Kontoblättern. Für die Zuordnung der Belege und 
Aufzeichnungen erfasst der Unternehmer ausschließlich die Kontoauszugsnummer. 
Allein anhand der Kontoauszugsnummer - ohne zusätzliche Angabe der Blattnummer 
und der Positionsnummer - ist eine Zuordnung von Beleg und Aufzeichnung oder 
Buchung in angemessener Zeit nicht nachprüfbar. 
4.3 Erfassungsgerechte Aufbereitung der Buchungsbelege 
75 
Eine erfassungsgerechte Aufbereitung der Buchungsbelege in Papierform oder die 
entsprechende Übernahme von Beleginformationen aus elektronischen Belegen 
(Daten, Datensätze, elektronische Dokumente und elektronische Unterlagen) ist 
sicherzustellen. Diese Aufbereitung der Belege ist insbesondere bei Fremdbelegen von 
Bedeutung, da der Steuerpflichtige im Allgemeinen keinen Einfluss auf die Gestaltung 
der ihm zugesandten Handels- und Geschäftsbriefe (z. B. Eingangsrechnungen) hat. 
 
76 
Werden neben bildhaften Urschriften auch elektronische Meldungen bzw. Datensätze 
ausgestellt (identische Mehrstücke derselben Belegart), ist die Aufbewahrung der 
tatsächlich weiterverarbeiteten Formate (buchungsbegründende Belege) ausreichend, 
sofern diese über die höchste maschinelle Auswertbarkeit verfügen. In diesem Fall 
erfüllt das Format mit der höchsten maschinellen Auswertbarkeit mit dessen 
vollständigem Dateninhalt die Belegfunktion und muss mit dessen vollständigem 
Inhalt gespeichert werden. Andernfalls sind beide Formate aufzubewahren. Dies gilt 
entsprechend, wenn mehrere elektronische Meldungen bzw. mehrere Datensätze ohne 
bildhafte Urschrift ausgestellt werden. Dies gilt auch für elektronische Meldungen 
(strukturierte Daten, wie z. B. ein monatlicher Kontoauszug im CSV-Format oder als 
XML-File), für die inhaltsgleiche bildhafte Dokumente zusätzlich bereitgestellt 
werden. Eine zusätzliche Archivierung der inhaltsgleichen Kontoauszüge in PDF oder 
Papier kann bei Erfüllung der Belegfunktion durch die strukturierten 
Kontoumsatzdaten entfallen.  
Bei Einsatz eines Fakturierungsprogramms muss unter Berücksichtigung der vorge-
nannten Voraussetzungen keine bildhafte Kopie der Ausgangsrechnung (z. B. in Form 


--- Page 19 ---
 
Seite 19
einer PDF-Datei) ab Erstellung gespeichert bzw. aufbewahrt werden, wenn jederzeit 
auf Anforderung ein entsprechendes Doppel der Ausgangsrechnung erstellt werden 
kann. 
Hierfür sind u. a. folgende Voraussetzungen zu beachten: 
• Entsprechende Stammdaten (z. B. Debitoren, Warenwirtschaft etc.) werden 
laufend historisiert 
• AGB werden ebenfalls historisiert und aus der Verfahrensdokumentation ist 
ersichtlich, welche AGB bei Erstellung der Originalrechnung verwendet 
wurden 
• Originallayout des verwendeten Geschäftsbogens wird als Muster (Layer) 
gespeichert und bei Änderungen historisiert. Zudem ist aus der Verfahrens-
dokumentation ersichtlich, welches Format bei Erstellung der Originalrech-
nung verwendet wurde (idealerweise kann bei Ausdruck oder Lesbarmachung 
des Rechnungsdoppels dieses Originallayout verwendet werden). 
• Weiterhin sind die Daten des Fakturierungsprogramms in maschinell auswert-
barer Form und unveränderbar aufzubewahren. 
77 
Jedem Geschäftsvorfall muss ein Beleg zugrunde liegen, mit folgenden Inhalten: 
 
Bezeichnung 
Begründung 
Eindeutige Belegnummer (z. B. Index, 
Paginiernummer, Dokumenten-ID, 
fortlaufende 
Rechnungsausgangsnummer) 
  
Angabe zwingend (§ 146 Absatz 1 Satz 1 
AO, einzeln, vollständig, geordnet) 
Kriterium für Vollständigkeitskontrolle 
(Belegsicherung) 
Bei umfangreichem Beleganfall ist Zuord-
nung und Identifizierung regelmäßig nicht 
aus Belegdatum oder anderen Merkmalen 
eindeutig ableitbar. 
Sofern die Fremdbelegnummer eine ein-
deutige Zuordnung zulässt, kann auch 
diese verwendet werden. 
Belegaussteller und -empfänger 
Soweit dies zu den branchenüblichen Min-
destaufzeichnungspflichten gehört und 
keine Aufzeichnungserleichterungen 
bestehen (z. B. § 33 UStDV) 
Betrag bzw. Mengen- oder Wertanga-
ben, aus denen sich der zu buchende 
Angabe zwingend (BFH vom 12. Mai 
1966, BStBl III S. 371); Dokumentation 


--- Page 20 ---
 
Seite 20
Bezeichnung 
Begründung 
Betrag ergibt 
einer Veränderung des Anlage- und 
Umlaufvermögens sowie des Eigen- und 
Fremdkapitals 
Währungsangabe und Wechselkurs bei 
Fremdwährung 
Ermittlung des Buchungsbetrags 
Hinreichende Erläuterung des 
Geschäftsvorfalls 
Hinweis auf BFH-Urteil vom 12. Mai 
1966, BStBl III S. 371; BFH-Urteil vom 
1. Oktober 1969, BStBl II 1970 S. 45 
 
Belegdatum 
 
Angabe zwingend (§ 146 Absatz 1 Satz 1 
AO, zeitgerecht). 
Identifikationsmerkmale für eine 
chronologische Erfassung, bei 
Bargeschäften regelmäßig Zeitpunkt des 
Geschäftsvorfalls 
Evtl. zusätzliche Erfassung der Belegzeit 
bei umfangreichem Beleganfall 
erforderlich 
Verantwortlicher Aussteller, soweit 
vorhanden 
Z. B. Bediener der Kasse 
 
Vgl. Rz. 85 zu den Inhalten der Grund(buch)aufzeichnungen. 
Vgl. Rz. 94 zu den Inhalten des Journals.  
78 
Für umsatzsteuerrechtliche Zwecke können weitere Angaben erforderlich sein.  
Dazu gehören beispielsweise die Rechnungsangaben nach §§ 14, 14a UStG und § 33 
UStDV. 
79 
Buchungsbelege sowie abgesandte oder empfangene Handels- oder Geschäftsbriefe in 
Papierform oder in elektronischer Form enthalten darüber hinaus vielfach noch weitere 
Informationen, die zum Verständnis und zur Überprüfung der für die Besteuerung 
gesetzlich vorgeschriebenen Aufzeichnungen im Einzelfall von Bedeutung und damit 
ebenfalls aufzubewahren sind. Dazu gehören z. B.: 
• Mengen- oder Wertangaben zur Erläuterung des Buchungsbetrags, sofern nicht 
bereits unter Rz. 77 berücksichtigt, 


--- Page 21 ---
 
Seite 21
• Einzelpreis (z. B. zur Bewertung), 
• Valuta, Fälligkeit (z. B. zur Bewertung), 
• Angaben zu Skonti, Rabatten (z. B. zur Bewertung), 
• Zahlungsart (bar, unbar), 
• Angaben zu einer Steuerbefreiung.  
4.4 Besonderheiten 
80 
Bei DV-gestützten Prozessen wird der Nachweis der zutreffenden Abbildung von 
Geschäftsvorfällen oft nicht durch konventionelle Belege erbracht (z. B. Buchungen 
aus Fakturierungssätzen, die durch Multiplikation von Preisen mit entnommenen Men-
gen aus der Betriebsdatenerfassung gebildet werden). Die Erfüllung der Belegfunktion 
ist dabei durch die ordnungsgemäße Anwendung des jeweiligen Verfahrens wie folgt 
nachzuweisen: 
• Dokumentation der programminternen Vorschriften zur Generierung der 
Buchungen, 
• Nachweis oder Bestätigung, dass die in der Dokumentation enthaltenen Vorschrif-
ten einem autorisierten Änderungsverfahren unterlegen haben (u. a. Zugriffs-
schutz, Versionsführung, Test- und Freigabeverfahren), 
• Nachweis der Anwendung des genehmigten Verfahrens sowie 
• Nachweis der tatsächlichen Durchführung der einzelnen Buchungen. 
 
81 
Bei Dauersachverhalten sind die Ursprungsbelege Basis für die folgenden Automatik-
buchungen. Bei (monatlichen) AfA-Buchungen nach Anschaffung eines abnutzbaren 
Wirtschaftsguts ist der Anschaffungsbeleg mit der AfA-Bemessungsgrundlage und 
weiteren Parametern (z. B. Nutzungsdauer) aufbewahrungspflichtig. Aus der Verfah-
rensdokumentation und der ordnungsmäßigen Anwendung des Verfahrens muss der 
automatische Buchungsvorgang nachvollziehbar sein. 
5. Aufzeichnung der Geschäftsvorfälle in zeitlicher Reihenfolge und in sach-
licher Ordnung (Grund(buch)aufzeichnungen, Journal- und 
Kontenfunktion) 
82 
Der Steuerpflichtige hat organisatorisch und technisch sicherzustellen, dass die elek-
tronischen Buchungen und sonst erforderlichen elektronischen Aufzeichnungen 
einzeln, vollständig, richtig, zeitgerecht und geordnet vorgenommen werden (§ 146 
Absatz 1 Satz 1 AO, § 239 Absatz 2 HGB). Jede Buchung oder Aufzeichnung muss im 
Zusammenhang mit einem Beleg stehen (BFH-Urteil vom 24. Juni 1997, BStBl II 
1998 S. 51).  


--- Page 22 ---
 
Seite 22
83 
Bei der doppelten Buchführung müssen alle Geschäftsvorfälle in zeitlicher Reihen-
folge (Grund(buch)aufzeichnung, Journalfunktion) und in sachlicher Gliederung 
(Hauptbuch, Kontenfunktion, siehe unter 5.4) darstellbar sein. Im Hauptbuch bzw. bei 
der Kontenfunktion verursacht jeder Geschäftsvorfall eine Buchung auf mindestens 
zwei Konten (Soll- und Habenbuchung).  
84 
Die Erfassung der Geschäftsvorfälle in elektronischen Grund(buch)aufzeichnungen 
(siehe unter 5.1 und 5.2) und die Verbuchung im Journal (siehe unter 5.3) kann organi-
satorisch und zeitlich auseinanderfallen (z. B. Grund(buch)aufzeichnung in Form von 
Kassenauftragszeilen). Erfüllen die Erfassungen Belegfunktion bzw. dienen sie der 
Belegsicherung, dann ist eine unprotokollierte Änderung nicht mehr zulässig (vgl. 
Rzn. 58 und 59). In diesen Fällen gelten die Ordnungsvorschriften bereits mit der 
ersten Erfassung der Geschäftsvorfälle und der Daten und müssen über alle nachfol-
genden Prozesse erhalten bleiben (z. B. Übergabe von Daten aus Vor- in Haupt-
systeme). 
5.1 Erfassung in Grund(buch)aufzeichnungen 
85 
Die fortlaufende Aufzeichnung der Geschäftsvorfälle erfolgt zunächst in Papierform 
oder in elektronischen Grund(buch)aufzeichnungen (Grundaufzeichnungsfunktion), 
um die Belegsicherung und die Garantie der Unverlierbarkeit des Geschäftsvorfalls zu 
gewährleisten. Sämtliche Geschäftsvorfälle müssen der zeitlichen Reihenfolge nach 
und materiell mit ihrem richtigen und erkennbaren Inhalt festgehalten werden. 
 
Zu den aufzeichnungspflichtigen Inhalten gehören 
• die in Rzn. 77, 78 und 79 enthaltenen Informationen, 
• das Erfassungsdatum, soweit abweichend vom Buchungsdatum 
Begründung: 
o Angabe zwingend (§ 146 Absatz 1 Satz 1 AO, zeitgerecht), 
o Zeitpunkt der Buchungserfassung und -verarbeitung, 
o Angabe der „Festschreibung“ (Veränderbarkeit nur mit Protokollie-
rung) zwingend, soweit nicht Unveränderbarkeit automatisch mit Erfas-
sung und Verarbeitung in Grund(buch)aufzeichnung. 
 
Vgl. Rz. 94 zu den Inhalten des Journals.  
86 
Die Grund(buch)aufzeichnungen sind nicht an ein bestimmtes System gebunden.  
Jedes System, durch das die einzelnen Geschäftsvorfälle fortlaufend, vollständig und 
richtig festgehalten werden, so dass die Grundaufzeichnungsfunktion erfüllt wird, ist 
ordnungsmäßig (vgl. BFH-Urteil vom 26. März 1968, BStBl II S. 527 für Buchfüh-
rungspflichtige).  


--- Page 23 ---
 
Seite 23
5.2 Digitale Grund(buch)aufzeichnungen 
87 
Sowohl beim Einsatz von Haupt- als auch von Vor- oder Nebensystemen ist eine 
Verbuchung im Journal des Hauptsystems (z. B. Finanzbuchhaltung) bis zum Ablauf 
des folgenden Monats nicht zu beanstanden, wenn die einzelnen Geschäftsvorfälle 
bereits in einem Vor- oder Nebensystem die Grundaufzeichnungsfunktion erfüllen und 
die Einzeldaten aufbewahrt werden.  
88 
Durch Erfassungs-, Übertragungs- und Verarbeitungskontrollen ist sicherzustellen, 
dass alle Geschäftsvorfälle vollständig erfasst oder übermittelt werden und danach 
nicht unbefugt (d. h. nicht ohne Zugriffsschutzverfahren) und nicht ohne Nachweis des 
vorausgegangenen Zustandes verändert werden können. Die Durchführung der Kon-
trollen ist zu protokollieren. Die konkrete Ausgestaltung der Protokollierung ist 
abhängig von der Komplexität und Diversifikation der Geschäftstätigkeit und der 
Organisationsstruktur sowie des eingesetzten DV-Systems.  
89 
Neben den Daten zum Geschäftsvorfall selbst müssen auch alle für die Verarbeitung 
erforderlichen Tabellendaten (Stammdaten, Bewegungsdaten, Metadaten wie z. B. 
Grund- oder Systemeinstellungen, geänderte Parameter), deren Historisierung und 
Programme gespeichert sein. Dazu gehören auch Informationen zu Kriterien, die eine 
Abgrenzung zwischen den steuerrechtlichen, den handelsrechtlichen und anderen 
Buchungen (z. B. nachrichtliche Datensätze zu Fremdwährungen, alternative Bewer-
tungsmethoden, statistische Buchungen, GuV-Kontennullstellungen, Summenkonten) 
ermöglichen. 
5.3 Verbuchung im Journal (Journalfunktion) 
90 
Die Journalfunktion erfordert eine vollständige, zeitgerechte und formal richtige Erfas-
sung, Verarbeitung und Wiedergabe der eingegebenen Geschäftsvorfälle. Sie dient 
dem Nachweis der tatsächlichen und zeitgerechten Verarbeitung der Geschäftsvorfälle. 
91 
Werden die unter 5.1 genannten Voraussetzungen bereits mit fortlaufender Verbu-
chung im Journal erfüllt, ist eine zusätzliche Erfassung in Grund(buch)aufzeichnungen 
nicht erforderlich. Eine laufende Aufzeichnung unmittelbar im Journal genügt den 
Erfordernissen der zeitgerechten Erfassung in Grund(buch)aufzeichnungen (vgl. BFH-
Urteil vom 16. September 1964, BStBl III S. 654). Zeitversetzte Buchungen im 
Journal genügen nur dann, wenn die Geschäftsvorfälle vorher fortlaufend richtig und 
vollständig in Grundaufzeichnungen oder Grundbüchern aufgezeichnet werden.  
Die Funktion der Grund(buch)aufzeichnungen kann auf Dauer auch durch eine geord-
nete und übersichtliche Belegablage erfüllt werden (§ 239 Absatz 4 HGB, § 146 
Absatz 5 AO, H 5.2 „Grundbuchaufzeichnungen“ EStH; vgl. Rz. 46).  


--- Page 24 ---
 
Seite 24
92 
Die Journalfunktion ist nur erfüllt, wenn die gespeicherten Aufzeichnungen gegen 
Veränderung oder Löschung geschützt sind.  
93 
Fehlerhafte Buchungen können wirksam und nachvollziehbar durch Stornierungen 
oder Neubuchungen geändert werden (siehe unter 8.). Es besteht deshalb weder ein 
Bedarf noch die Notwendigkeit für weitere nachträgliche Veränderungen einer einmal 
erfolgten Buchung. Bei der doppelten Buchführung kann die Journalfunktion 
zusammen mit der Kontenfunktion erfüllt werden, indem bereits bei der erstmaligen 
Erfassung des Geschäftsvorfalls alle für die sachliche Zuordnung notwendigen 
Informationen erfasst werden. 
 
94 
Zur Erfüllung der Journalfunktion und zur Ermöglichung der Kontenfunktion sind bei 
der Buchung insbesondere die nachfolgenden Angaben zu erfassen oder bereit zu 
stellen: 
• Eindeutige Belegnummer (siehe Rz. 77), 
• Buchungsbetrag (siehe Rz. 77), 
• Währungsangabe und Wechselkurs bei Fremdwährung (siehe Rz. 77), 
• Hinreichende Erläuterung des Geschäftsvorfalls (siehe Rz. 77) - kann (bei 
Erfüllung der Journal- und Kontenfunktion) im Einzelfall bereits durch andere in 
Rz. 94 aufgeführte Angaben gegeben sein, 
• Belegdatum, soweit nicht aus den Grundaufzeichnungen ersichtlich (siehe Rzn. 77 
und 85) 
• Buchungsdatum, 
• Erfassungsdatum, soweit nicht aus der Grundaufzeichnung ersichtlich (siehe 
Rz. 85), 
• Autorisierung soweit vorhanden, 
• Buchungsperiode/Voranmeldungszeitraum (Ertragsteuer/Umsatzsteuer), 
• Umsatzsteuersatz (siehe Rz. 78), 
• Steuerschlüssel, soweit vorhanden (siehe Rz. 78), 
• Umsatzsteuerbetrag (siehe Rz. 78), 
• Umsatzsteuerkonto (siehe Rz. 78), 
• Umsatzsteuer-Identifikationsnummer (siehe Rz. 78), 
• Steuernummer (siehe Rz. 78), 
• Konto und Gegenkonto, 
• Buchungsschlüssel (soweit vorhanden), 
• Soll- und Haben-Betrag, 
• eindeutige Identifikationsnummer (Schlüsselfeld) des Geschäftsvorfalls (soweit 
Aufteilung der Geschäftsvorfälle in Teilbuchungssätze [Buchungs-Halbsätze] oder 
zahlreiche Soll- oder Habenkonten [Splitbuchungen] vorhanden). Über die einheit-


--- Page 25 ---
 
Seite 25
liche und je Wirtschaftsjahr eindeutige Identifikationsnummer des Geschäftsvor-
falls muss die Identifizierung und Zuordnung aller Teilbuchungen einschließlich 
Steuer-, Sammel-, Verrechnungs- und Interimskontenbuchungen eines Geschäfts-
vorfalls gewährleistet sein.  
5.4 Aufzeichnung der Geschäftsvorfälle in sachlicher Ordnung (Hauptbuch) 
95 
Die Geschäftsvorfälle sind so zu verarbeiten, dass sie geordnet darstellbar sind 
(Kontenfunktion) und damit die Grundlage für einen Überblick über die Vermögens- 
und Ertragslage darstellen. Zur Erfüllung der Kontenfunktion bei Bilanzierenden 
müssen Geschäftsvorfälle nach Sach- und Personenkonten geordnet dargestellt 
werden.  
96 
Die Kontenfunktion verlangt, dass die im Journal in zeitlicher Reihenfolge einzeln 
aufgezeichneten Geschäftsvorfälle auch in sachlicher Ordnung auf Konten dargestellt 
werden. Damit bei Bedarf für einen zurückliegenden Zeitpunkt ein Zwischenstatus 
oder eine Bilanz mit Gewinn- und Verlustrechnung aufgestellt werden kann, müssen 
Eröffnungsbilanzbuchungen und alle Abschlussbuchungen in den Konten enthalten 
sein. Die Konten sind nach Abschlussposition zu sammeln und nach Kontensummen 
oder Salden fortzuschreiben.  
97 
Werden innerhalb verschiedener Bereiche des DV-Systems oder zwischen unter-
schiedlichen DV-Systemen differierende Ordnungskriterien verwendet, so müssen 
entsprechende Zuordnungstabellen (z. B. elektronische Mappingtabellen) vorgehalten 
werden (z. B. Wechsel des Kontenrahmens, unterschiedliche Nummernkreise in Vor- 
und Hauptsystem). Dies gilt auch bei einer elektronischen Übermittlung von Daten an 
die Finanzbehörde (z. B. unterschiedliche Ordnungskriterien in Bilanz/GuV und EÜR 
einerseits und USt-Voranmeldung, LSt-Anmeldung, Anlage EÜR und E-Bilanz ande-
rerseits). Sollte die Zuordnung mit elektronischen Verlinkungen oder Schlüsselfeldern 
erfolgen, sind die Verlinkungen in dieser Form vorzuhalten.  
98 
Die vorstehenden Ausführungen gelten für die Nebenbücher entsprechend. 
99 
Bei der Übernahme verdichteter Zahlen ins Hauptsystem müssen die zugehörigen Ein-
zelaufzeichnungen aus den Vor- und Nebensystemen erhalten bleiben. 
6. Internes Kontrollsystem (IKS) 
100 
Für die Einhaltung der Ordnungsvorschriften des § 146 AO (siehe unter 3.) hat der 
Steuerpflichtige Kontrollen einzurichten, auszuüben und zu protokollieren.  
Hierzu gehören beispielsweise 
• Zugangs- und Zugriffsberechtigungskontrollen auf Basis entsprechender Zugangs- 
und Zugriffsberechtigungskonzepte (z. B. spezifische Zugangs- und 


--- Page 26 ---
 
Seite 26
Zugriffsberechtigungen), 
• Funktionstrennungen, 
• Erfassungskontrollen (Fehlerhinweise, Plausibilitätsprüfungen), 
• Abstimmungskontrollen bei der Dateneingabe, 
• Verarbeitungskontrollen, 
• Schutzmaßnahmen gegen die beabsichtigte und unbeabsichtigte Verfälschung von 
Programmen, Daten und Dokumenten. 
Die konkrete Ausgestaltung des Kontrollsystems ist abhängig von der Komplexität 
und Diversifikation der Geschäftstätigkeit und der Organisationsstruktur sowie des 
eingesetzten DV-Systems. 
101 
Im Rahmen eines funktionsfähigen IKS muss auch anlassbezogen (z. B. System-
wechsel) geprüft werden, ob das eingesetzte DV-System tatsächlich dem dokumen-
tierten System entspricht (siehe Rz. 155 zu den Rechtsfolgen bei fehlender oder unge-
nügender Verfahrensdokumentation). 
102 
Die Beschreibung des IKS ist Bestandteil der Verfahrensdokumentation (siehe 
unter 10.1). 
7. Datensicherheit 
103 
Der Steuerpflichtige hat sein DV-System gegen Verlust (z. B. Unauffindbarkeit, Ver-
nichtung, Untergang und Diebstahl) zu sichern und gegen unberechtigte Eingaben und 
Veränderungen (z. B. durch Zugangs- und Zugriffskontrollen) zu schützen. 
104 
Werden die Daten, Datensätze, elektronischen Dokumente und elektronischen Unter-
lagen nicht ausreichend geschützt und können deswegen nicht mehr vorgelegt werden, 
so ist die Buchführung formell nicht mehr ordnungsmäßig. 
105 
Beispiel 6: 
Unternehmer überschreibt unwiderruflich die Finanzbuchhaltungsdaten des Vorjahres 
mit den Daten des laufenden Jahres. 
Die sich daraus ergebenden Rechtsfolgen sind vom jeweiligen Einzelfall abhängig. 
106 
Die Beschreibung der Vorgehensweise zur Datensicherung ist Bestandteil der Verfah-
rensdokumentation (siehe unter 10.1). Die konkrete Ausgestaltung der Beschreibung 
ist abhängig von der Komplexität und Diversifikation der Geschäftstätigkeit und der 
Organisationsstruktur sowie des eingesetzten DV-Systems. 


--- Page 27 ---
 
Seite 27
8. Unveränderbarkeit, Protokollierung von Änderungen 
107 
Nach § 146 Absatz 4 AO darf eine Buchung oder Aufzeichnung nicht in einer Weise 
verändert werden, dass der ursprüngliche Inhalt nicht mehr feststellbar ist. Auch sol-
che Veränderungen dürfen nicht vorgenommen werden, deren Beschaffenheit es unge-
wiss lässt, ob sie ursprünglich oder erst später gemacht worden sind. 
108 
Das zum Einsatz kommende DV-Verfahren muss die Gewähr dafür bieten, dass alle 
Informationen (Programme und Datenbestände), die einmal in den Verarbeitungs-
prozess eingeführt werden (Beleg, Grundaufzeichnung, Buchung), nicht mehr unter-
drückt oder ohne Kenntlichmachung überschrieben, gelöscht, geändert oder verfälscht 
werden können. Bereits in den Verarbeitungsprozess eingeführte Informationen 
(Beleg, Grundaufzeichnung, Buchung) dürfen nicht ohne Kenntlichmachung durch 
neue Daten ersetzt werden. 
109 
Beispiele 7 für unzulässige Vorgänge: 
• Elektronische Grund(buch)aufzeichnungen aus einem Kassen- oder Warenwirt-
schaftssystem werden über eine Datenschnittstelle in ein Officeprogramm expor-
tiert, dort unprotokolliert editiert und anschließend über eine Datenschnittstelle 
reimportiert. 
• Vorerfassungen und Stapelbuchungen werden bis zur Erstellung des Jahresab-
schlusses und darüber hinaus offen gehalten. Alle Eingaben können daher 
unprotokolliert geändert werden. 
 
110 
Die Unveränderbarkeit der Daten, Datensätze, elektronischen Dokumente und elektro-
nischen Unterlagen (vgl. Rzn. 3 bis 5) kann sowohl hardwaremäßig (z. B. unveränder-
bare und fälschungssichere Datenträger) als auch softwaremäßig (z. B. Sicherungen, 
Sperren, Festschreibung, Löschmerker, automatische Protokollierung, Historisierun-
gen, Versionierungen) als auch organisatorisch (z. B. mittels Zugriffsberechtigungs-
konzepten) gewährleistet werden. Die Ablage von Daten und elektronischen Doku-
menten in einem Dateisystem erfüllt die Anforderungen der Unveränderbarkeit 
regelmäßig nicht, soweit nicht zusätzliche Maßnahmen ergriffen werden, die eine 
Unveränderbarkeit gewährleisten.  
111 
Spätere Änderungen sind ausschließlich so vorzunehmen, dass sowohl der ursprüng-
liche Inhalt als auch die Tatsache, dass Veränderungen vorgenommen wurden, 
erkennbar bleiben. Bei programmgenerierten bzw. programmgesteuerten Aufzeich-
nungen (automatisierte Belege bzw. Dauerbelege) sind Änderungen an den der Auf-
zeichnung zugrunde liegenden Generierungs- und Steuerungsdaten ebenfalls aufzu-
zeichnen. Dies betrifft insbesondere die Protokollierung von Änderungen in Einstel-
lungen oder die Parametrisierung der Software. Bei einer Änderung von Stammdaten 


--- Page 28 ---
 
Seite 28
(z. B. Abkürzungs- oder Schlüsselverzeichnisse, Organisationspläne) muss die ein-
deutige Bedeutung in den entsprechenden Bewegungsdaten (z. B. Umsatzsteuer-
schlüssel, Währungseinheit, Kontoeigenschaft) erhalten bleiben. Ggf. müssen Stamm-
datenänderungen ausgeschlossen oder Stammdaten mit Gültigkeitsangaben historisiert 
werden, um mehrdeutige Verknüpfungen zu verhindern. Auch eine Änderungshistorie 
darf nicht nachträglich veränderbar sein. 
112 
Werden Systemfunktionalitäten oder Manipulationsprogramme eingesetzt, die diesen 
Anforderungen entgegenwirken, führt dies zur Ordnungswidrigkeit der elektronischen 
Bücher und sonst erforderlicher elektronischer Aufzeichnungen. 
Beispiel 8: 
 
Einsatz von Zappern, Phantomware, Backofficeprodukten mit dem Ziel unproto-
kollierter Änderungen elektronischer Einnahmenaufzeichnungen. 
9. Aufbewahrung  
113 
Der sachliche Umfang der Aufbewahrungspflicht in § 147 Absatz 1 AO besteht 
grundsätzlich nur im Umfang der Aufzeichnungspflicht (BFH-Urteil vom 24. Juni 
2009, BStBl II 2010 S. 452; BFH-Urteil vom 26. Februar 2004, BStBl II S. 599). 
114 
Müssen Bücher für steuerliche Zwecke geführt werden, sind sie in vollem Umfang 
aufbewahrungs- und vorlagepflichtig (z. B. Finanzbuchhaltung hinsichtlich Drohver-
lustrückstellungen, nicht abziehbare Betriebsausgaben, organschaftliche Steuer-
umlagen; BFH-Beschluss vom 26. September 2007, BStBl II 2008 S. 415). 
115 
Auch Steuerpflichtige, die nach § 4 Absatz 3 EStG als Gewinn den Überschuss der 
Betriebseinnahmen über die Betriebsausgaben ansetzen, sind verpflichtet, Aufzeich-
nungen und Unterlagen nach § 147 Absatz 1 AO aufzubewahren (BFH-Urteil vom 
24. Juni 2009, BStBl II 2010 S. 452; BFH-Urteil vom 26. Februar 2004, BStBl II 
S. 599). 
116 
Aufbewahrungspflichten können sich auch aus anderen Rechtsnormen (z. B. § 14b 
UStG) ergeben. 
117 
Die aufbewahrungspflichtigen Unterlagen müssen geordnet aufbewahrt werden. Ein 
bestimmtes Ordnungssystem ist nicht vorgeschrieben. Die Ablage kann z. B. nach 
Zeitfolge, Sachgruppen, Kontenklassen, Belegnummern oder alphabetisch erfolgen. 
Bei elektronischen Unterlagen ist ihr Eingang, ihre Archivierung und ggf. Konver-
tierung sowie die weitere Verarbeitung zu protokollieren. Es muss jedoch sicherge-
stellt sein, dass ein sachverständiger Dritter innerhalb angemessener Zeit prüfen kann. 
118 
Die nach außersteuerlichen und steuerlichen Vorschriften aufzeichnungspflichtigen 
und nach § 147 Absatz 1 AO aufbewahrungspflichtigen Unterlagen können nach § 147 
Absatz 2 AO bis auf wenige Ausnahmen auch als Wiedergabe auf einem Bildträger 


--- Page 29 ---
 
Seite 29
oder auf anderen Datenträgern aufbewahrt werden, wenn dies den GoB entspricht und 
sichergestellt ist, dass die Wiedergabe oder die Daten 
1. mit den empfangenen Handels- oder Geschäftsbriefen und den Buchungsbelegen 
bildlich und mit den anderen Unterlagen inhaltlich übereinstimmen, wenn sie 
lesbar gemacht werden, 
1. während der Dauer der Aufbewahrungsfrist jederzeit verfügbar sind, unverzüglich 
lesbar gemacht und maschinell ausgewertet werden können. 
119 
Sind aufzeichnungs- und aufbewahrungspflichtige Daten, Datensätze, elektronische 
Dokumente und elektronische Unterlagen im Unternehmen entstanden oder dort ein-
gegangen, sind sie auch in dieser Form aufzubewahren und dürfen vor Ablauf der Auf-
bewahrungsfrist nicht gelöscht werden. Sie dürfen daher nicht mehr ausschließlich in 
ausgedruckter Form aufbewahrt werden und müssen für die Dauer der Aufbewah-
rungsfrist unveränderbar erhalten bleiben (z. B. per E-Mail eingegangene Rechnung im 
PDF-Format oder bildlich erfasste Papierbelege). Dies gilt unabhängig davon, ob die 
Aufbewahrung im Produktivsystem oder durch Auslagerung in ein anderes DV-System 
erfolgt. Unter Zumutbarkeitsgesichtspunkten ist es nicht zu beanstanden, wenn der 
Steuerpflichtige elektronisch erstellte und in Papierform abgesandte Handels- und 
Geschäftsbriefe nur in Papierform aufbewahrt. 
120 
Beispiel 9 zu Rz. 119: 
Ein Steuerpflichtiger erstellt seine Ausgangsrechnungen mit einem Textverarbeitungs-
programm. Nach dem Ausdruck der jeweiligen Rechnung wird die hierfür verwendete 
Maske (Dokumentenvorlage) mit den Inhalten der nächsten Rechnung überschrieben. 
Es ist in diesem Fall nicht zu beanstanden, wenn das Doppel des versendeten Schrei-
bens in diesem Fall nur als Papierdokument aufbewahrt wird. Werden die abgesandten 
Handels- und Geschäftsbriefe jedoch tatsächlich in elektronischer Form aufbewahrt 
(z. B. im File-System oder einem DMS-System), so ist eine ausschließliche Aufbe-
wahrung in Papierform nicht mehr zulässig. Das Verfahren muss dokumentiert wer-
den. Werden Handels- oder Geschäftsbriefe mit Hilfe eines Fakturierungssystems oder 
ähnlicher Anwendungen erzeugt, bleiben die elektronischen Daten aufbewahrungs-
pflichtig. 
121 
Bei den Daten und Dokumenten ist - wie bei den Informationen in Papierbelegen - auf 
deren Inhalt und auf deren Funktion abzustellen, nicht auf deren Bezeichnung. So sind 
beispielsweise E-Mails mit der Funktion eines Handels- oder Geschäftsbriefs oder 
eines Buchungsbelegs in elektronischer Form aufbewahrungspflichtig. Dient eine 
E-Mail nur als „Transportmittel“, z. B. für eine angehängte elektronische Rechnung, 
und enthält darüber hinaus keine weitergehenden aufbewahrungspflichtigen Informa-
tionen, so ist diese nicht aufbewahrungspflichtig (wie der bisherige Papierbriefum-
schlag). 


--- Page 30 ---
 
Seite 30
122 
Ein elektronisches Dokument ist mit einem nachvollziehbaren und eindeutigen Index 
zu versehen. Der Erhalt der Verknüpfung zwischen Index und elektronischem Doku-
ment muss während der gesamten Aufbewahrungsfrist gewährleistet sein. Es ist 
sicherzustellen, dass das elektronische Dokument unter dem zugeteilten Index ver-
waltet werden kann. Stellt ein Steuerpflichtiger durch organisatorische Maßnahmen 
sicher, dass das elektronische Dokument auch ohne Index verwaltet werden kann, und 
ist dies in angemessener Zeit nachprüfbar, so ist aus diesem Grund die Buchführung 
nicht zu beanstanden.  
123 
Das Anbringen von Buchungsvermerken, Indexierungen, Barcodes, farblichen Hervor-
hebungen usw. darf - unabhängig von seiner technischen Ausgestaltung - keinen Ein-
fluss auf die Lesbarmachung des Originalzustands haben. Die elektronischen Bearbei-
tungsvorgänge sind zu protokollieren und mit dem elektronischen Dokument zu 
speichern, damit die Nachvollziehbarkeit und Prüfbarkeit des Originalzustands und 
seiner Ergänzungen gewährleistet ist. 
124 
Hinsichtlich der Aufbewahrung digitaler Unterlagen bei Bargeschäften wird auf das 
BMF-Schreiben vom 26. November 2010 (IV A 4 - S 0316/08/10004-07, BStBl I 
S. 1342) hingewiesen. 
9.1 Maschinelle Auswertbarkeit (§ 147 Absatz 2 Nummer 2 AO) 
125 
Art und Umfang der maschinellen Auswertbarkeit sind nach den tatsächlichen 
Informations- und Dokumentationsmöglichkeiten zu beurteilen. 
Beispiel 10: 
Datenformat für elektronische Rechnungen ZUGFeRD (Zentraler User Guide des 
Forums elektronische Rechnung Deutschland) 
Hier ist vorgesehen, dass Rechnungen im PDF/A-3-Format versendet werden. Diese 
bestehen aus einem Rechnungsbild (dem augenlesbaren, sichtbaren Teil der PDF-
Datei) und den in die PDF-Datei eingebetteten Rechnungsdaten im standardisierten 
XML-Format. 
Entscheidend ist hier jetzt nicht, ob der Rechnungsempfänger nur das Rechnungsbild 
(Image) nutzt, sondern, dass auch noch tatsächlich XML-Daten vorhanden sind, die 
nicht durch eine Formatumwandlung (z. B. in TIFF) gelöscht werden dürfen.  
Die maschinelle Auswertbarkeit bezieht sich auf sämtliche Inhalte der PDF/A-3-Datei. 
126 
Eine maschinelle Auswertbarkeit ist nach diesem Beurteilungsmaßstab bei aufzeich-
nungs- und aufbewahrungspflichtigen Daten, Datensätzen, elektronischen Dokumen-
ten und elektronischen Unterlagen (vgl. Rzn. 3 bis 5) u. a. gegeben, die 
• mathematisch-technische Auswertungen ermöglichen, 
• eine Volltextsuche ermöglichen, 
• auch ohne mathematisch-technische Auswertungen eine Prüfung im weitesten 


--- Page 31 ---
 
Seite 31
Sinne ermöglichen (z. B. Bildschirmabfragen, die Nachverfolgung von 
Verknüpfungen und Verlinkungen oder die Textsuche nach bestimmten 
Eingabekriterien).  
127 
Mathematisch-technische Auswertung bedeutet, dass alle in den aufzeichnungs- und 
aufbewahrungspflichtigen Daten, Datensätzen, elektronischen Dokumenten und 
elektronischen Unterlagen (vgl. Rzn. 3 bis 5) enthaltenen Informationen automatisiert 
(DV-gestützt) interpretiert, dargestellt, verarbeitet sowie für andere Datenbank-
anwendungen und eingesetzte Prüfsoftware direkt, ohne weitere Konvertierungs- und 
Bearbeitungsschritte und ohne Informationsverlust nutzbar gemacht werden können 
(z. B. für wahlfreie Sortier-, Summier-, Verbindungs- und Filterungsmöglichkeiten). 
Mathematisch-technische Auswertungen sind z. B. möglich bei: 
• Elektronischen Grund(buch)aufzeichnungen (z. B. Kassendaten, Daten aus Waren-
wirtschaftssystem, Inventurlisten), 
• Journaldaten aus Finanzbuchhaltung oder Lohnbuchhaltung, 
• Textdateien oder Dateien aus Tabellenkalkulationen mit strukturierten Daten in 
tabellarischer Form (z. B. Reisekostenabrechnung, Überstundennachweise). 
128 
Neben den Daten in Form von Datensätzen und den elektronischen Dokumenten sind 
auch alle zur maschinellen Auswertung der Daten im Rahmen des Datenzugriffs not-
wendigen Strukturinformationen (z. B. über die Dateiherkunft [eingesetztes System], 
die Dateistruktur, die Datenfelder, verwendete Zeichensatztabellen) in maschinell 
auswertbarer Form sowie die internen und externen Verknüpfungen vollständig und in 
unverdichteter, maschinell auswertbarer Form aufzubewahren. Im Rahmen einer 
Datenträgerüberlassung ist der Erhalt technischer Verlinkungen auf dem Datenträger 
nicht erforderlich, sofern dies nicht möglich ist. 
129 
Die Reduzierung einer bereits bestehenden maschinellen Auswertbarkeit, beispiels-
weise durch Umwandlung des Dateiformats oder der Auswahl bestimmter Aufbewah-
rungsformen, ist nicht zulässig (siehe unter 9.2). 
Beispiele 11: 
• Umwandlung von PDF/A-Dateien ab der Norm PDF/A-3 in ein Bildformat (z. B. 
TIFF, JPEG etc.), da dann die in den PDF/A-Dateien enthaltenen XML-Daten und 
ggf. auch vorhandene Volltextinformationen gelöscht werden. 
• Umwandlung von elektronischen Grund(buch)aufzeichnungen (z. B. Kasse, 
Warenwirtschaft) in ein PDF-Format. 
• Umwandlung von Journaldaten einer Finanzbuchhaltung oder Lohnbuchhaltung in 
ein PDF-Format. 
Eine Umwandlung in ein anderes Format (z. B. Inhouse-Format) ist zulässig, wenn die 
maschinelle Auswertbarkeit nicht eingeschränkt wird und keine inhaltliche Verände-
rung vorgenommen wird (siehe Rz. 135). 


--- Page 32 ---
 
Seite 32
Der Steuerpflichtige muss dabei auch berücksichtigen, dass entsprechende Einschrän-
kungen in diesen Fällen zu seinen Lasten gehen können (z. B. Speicherung einer 
E-Mail als PDF-Datei. Die Informationen des Headers [z. B. Informationen zum 
Absender] gehen dabei verloren und es ist nicht mehr nachvollziehbar, wie der tat-
sächliche Zugang der E-Mail erfolgt ist). 
9.2 Elektronische Aufbewahrung 
130 
Werden Handels- oder Geschäftsbriefe und Buchungsbelege in Papierform empfangen 
und danach elektronisch bildlich erfasst (z. B. gescannt oder fotografiert), ist das 
hierdurch entstandene elektronische Dokument so aufzubewahren, dass die Wieder-
gabe mit dem Original bildlich übereinstimmt, wenn es lesbar gemacht wird (§ 147 
Absatz  2 AO). Eine bildliche Erfassung kann hierbei mit den verschiedensten Arten 
von Geräten (z. B. Smartphones, Multifunktionsgeräten oder Scan-Straßen) erfolgen, 
wenn die Anforderungen dieses Schreibens erfüllt sind. Werden bildlich erfasste 
Dokumente per Optical-Character-Recognition-Verfahren (OCR-Verfahren) um 
Volltextinformationen angereichert (zum Beispiel volltextrecherchierbare PDFs), so 
ist dieser Volltext nach Verifikation und Korrektur über die Dauer der Aufbewah-
rungsfrist aufzubewahren und auch für Prüfzwecke verfügbar zu machen. § 146 
Absatz 2 AO steht einer bildlichen Erfassung durch mobile Geräte (z. B. Smartphones) 
im Ausland nicht entgegen, wenn die Belege im Ausland entstanden sind bzw. 
empfangen wurden und dort direkt erfasst werden (z. B. bei Belegen über eine 
Dienstreise im Ausland). 
131 
Eingehende elektronische Handels- oder Geschäftsbriefe und Buchungsbelege müssen 
in dem Format aufbewahrt werden, in dem sie empfangen wurden (z. B. Rechnungen 
oder Kontoauszüge im PDF- oder Bildformat). Eine Umwandlung in ein anderes 
Format (z. B. MSG in PDF) ist dann zulässig, wenn die maschinelle Auswertbarkeit 
nicht eingeschränkt wird und keine inhaltlichen Veränderungen vorgenommen werden 
(siehe Rz. 135). Erfolgt eine Anreicherung der Bildinformationen, z. B. durch OCR 
(Beispiel: Erzeugung einer volltextrecherchierbaren PDF-Datei im Erfassungsprozess), 
sind die dadurch gewonnenen Informationen nach Verifikation und Korrektur 
ebenfalls aufzubewahren. 
132 
Im DV-System erzeugte Daten im Sinne der Rzn. 3 bis 5 (z. B. Grund(buch)aufzeich-
nungen in Vor- und Nebensystemen, Buchungen, generierte Datensätze zur Erstellung 
von Ausgangsrechnungen) oder darin empfangene Daten (z. B. EDI-Verfahren) 
müssen im Ursprungsformat aufbewahrt werden. 
133 
Im DV-System erzeugte Dokumente (z. B. als Textdokumente erstellte Ausgangs-
rechnungen [§ 14b UStG], elektronisch abgeschlossene Verträge, Handels- und 
Geschäftsbriefe, Verfahrensdokumentation) sind im Ursprungsformat aufzubewahren. 


--- Page 33 ---
 
Seite 33
Unter Zumutbarkeitsgesichtspunkten ist es nicht zu beanstanden, wenn der Steuer-
pflichtige elektronisch erstellte und in Papierform abgesandte Handels- und Geschäfts-
briefe nur in Papierform aufbewahrt (Hinweis auf Rzn. 119, 120). Eine Umwandlung 
in ein anderes Format (z. B. Inhouse-Format) ist zulässig, wenn die maschinelle Aus-
wertbarkeit nicht eingeschränkt wird und keine inhaltliche Veränderung vorgenommen 
wird (siehe Rz. 135).  
134 
Bei Einsatz von Kryptografietechniken ist sicherzustellen, dass die verschlüsselten 
Unterlagen im DV-System in entschlüsselter Form zur Verfügung stehen.  
Werden Signaturprüfschlüssel verwendet, sind die eingesetzten Schlüssel aufzu-
bewahren. Die Aufbewahrungspflicht endet, wenn keine der mit den Schlüsseln 
signierten Unterlagen mehr aufbewahrt werden müssen. 
135 
Bei Umwandlung (Konvertierung) aufbewahrungspflichtiger Unterlagen in ein unter-
nehmenseigenes Format (sog. Inhouse-Format) sind beide Versionen zu archivieren, 
derselben Aufzeichnung zuzuordnen und mit demselben Index zu verwalten sowie die 
konvertierte Version als solche zu kennzeichnen.  
Die Aufbewahrung beider Versionen ist bei Beachtung folgender Anforderungen nicht 
erforderlich, sondern es ist die Aufbewahrung der konvertierten Fassung ausreichend: 
• Es wird keine bildliche oder inhaltliche Veränderung vorgenommen. 
• Bei der Konvertierung gehen keine sonstigen aufbewahrungspflichtigen 
Informationen verloren. 
• Die ordnungsgemäße und verlustfreie Konvertierung wird dokumentiert 
(Verfahrensdokumentation). 
• Die maschinelle Auswertbarkeit und der Datenzugriff durch die Finanzbehörde 
werden nicht eingeschränkt; dabei ist es zulässig, wenn bei der Konvertierung 
Zwischenaggregationsstufen nicht gespeichert, aber in der Verfahrensdokumen-
tation so dargestellt werden, dass die retrograde und progressive Prüfbarkeit 
sichergestellt ist. 
 
Nicht aufbewahrungspflichtig sind die während der maschinellen Verarbeitung durch 
das Buchführungssystem erzeugten Dateien, sofern diese ausschließlich einer 
temporären Zwischenspeicherung von Verarbeitungsergebnissen dienen und deren 
Inhalte im Laufe des weiteren Verarbeitungsprozesses vollständig Eingang in die 
Buchführungsdaten finden. Voraussetzung ist jedoch, dass bei der weiteren Verarbei-
tung keinerlei „Verdichtung“ aufzeichnungs- und aufbewahrungspflichtiger Daten 
(vgl. Rzn. 3 bis 5) vorgenommen wird. 


--- Page 34 ---
 
Seite 34
9.3 Bildliche Erfassung von Papierdokumenten  
136 
Papierdokumente werden durch die bildliche Erfassung (siehe Rz. 130) in elektroni-
sche Dokumente umgewandelt. Das Verfahren muss dokumentiert werden.  
Der Steuerpflichtige sollte daher eine Organisationsanweisung erstellen, die unter 
anderem regelt: 
• wer erfassen darf, 
• zu welchem Zeitpunkt erfasst wird oder erfasst werden soll (z. B. beim 
Posteingang, während oder nach Abschluss der Vorgangsbearbeitung), 
• welches Schriftgut erfasst wird, 
• ob eine bildliche oder inhaltliche Übereinstimmung mit dem Original erforderlich ist,  
• wie die Qualitätskontrolle auf Lesbarkeit und Vollständigkeit und 
• wie die Protokollierung von Fehlern zu erfolgen hat. 
 
Die konkrete Ausgestaltung dieser Verfahrensdokumentation ist abhängig von der 
Komplexität und Diversifikation der Geschäftstätigkeit und der Organisationsstruktur 
sowie des eingesetzten DV-Systems.  
Aus Vereinfachungsgründen (z. B. bei Belegen über eine Dienstreise im Ausland) 
steht § 146 Absatz 2 AO einer bildlichen Erfassung durch mobile Geräte (z. B. 
Smartphones) im Ausland nicht entgegen, wenn die Belege im Ausland entstanden 
sind bzw. empfangen wurden und dort direkt erfasst werden.  
Erfolgt im Zusammenhang mit einer, nach § 146 Absatz 2a AO genehmigten, 
Verlagerung der elektronischen Buchführung ins Ausland eine ersetzende bildliche 
Erfassung, wird es nicht beanstandet, wenn die papierenen Ursprungsbelege zu diesem 
Zweck an den Ort der elektronischen Buchführung verbracht werden. Die bildliche 
Erfassung hat zeitnah zur Verbringung der Papierbelege ins Ausland zu erfolgen. 
 
137 
Eine vollständige Farbwiedergabe ist erforderlich, wenn der Farbe Beweisfunktion 
zukommt (z. B. Minusbeträge in roter Schrift, Sicht-, Bearbeitungs- und Zeichnungs-
vermerke in unterschiedlichen Farben). 
 
138 
Für Besteuerungszwecke ist eine elektronische Signatur oder ein Zeitstempel nicht 
erforderlich.  
139 
Im Anschluss an den Erfassungsvorgang (siehe Rz. 130) darf die weitere Bearbeitung 
nur mit dem elektronischen Dokument erfolgen. Die Papierbelege sind dem weiteren 
Bearbeitungsgang zu entziehen, damit auf diesen keine Bemerkungen, Ergänzungen 
usw. vermerkt werden können, die auf dem elektronischen Dokument nicht enthalten 
sind. Sofern aus organisatorischen Gründen nach dem Erfassungsvorgang eine weitere 
Vorgangsbearbeitung des Papierbeleges erfolgt, muss nach Abschluss der Bearbeitung 


--- Page 35 ---
 
Seite 35
der bearbeitete Papierbeleg erneut erfasst und ein Bezug zur ersten elektronischen 
Fassung des Dokuments hergestellt werden (gemeinsamer Index).  
140 
Nach der bildlichen Erfassung im Sinne der Rz. 130 dürfen Papierdokumente vernich-
tet werden, soweit sie nicht nach außersteuerlichen oder steuerlichen Vorschriften im 
Original aufzubewahren sind. Der Steuerpflichtige muss entscheiden, ob Dokumente, 
deren Beweiskraft bei der Aufbewahrung in elektronischer Form nicht erhalten bleibt, 
zusätzlich in der Originalform aufbewahrt werden sollen.  
141 
Der Verzicht auf einen Papierbeleg darf die Möglichkeit der Nachvollziehbarkeit und 
Nachprüfbarkeit nicht beeinträchtigen. 
9.4 Auslagerung von Daten aus dem Produktivsystem und Systemwechsel 
142 
Im Falle eines Systemwechsels (z. B. Abschaltung Altsystem, Datenmigration), einer 
Systemänderung (z. B. Änderung der OCR-Software, Update der Finanzbuchhaltung 
etc.) oder einer Auslagerung von aufzeichnungs- und aufbewahrungspflichtigen Daten 
(vgl. Rzn. 3 bis 5) aus dem Produktivsystem ist es nur dann nicht erforderlich, die 
ursprüngliche Hard- und Software des Produktivsystems über die Dauer der Aufbe-
wahrungsfrist vorzuhalten, wenn die folgenden Voraussetzungen erfüllt sind: 
1. Die aufzeichnungs- und aufbewahrungspflichtigen Daten (einschließlich 
Metadaten, Stammdaten, Bewegungsdaten und der erforderlichen 
Verknüpfungen) müssen unter Beachtung der Ordnungsvorschriften (vgl. 
§§ 145 bis 147 AO) quantitativ und qualitativ gleichwertig in ein neues System, 
in eine neue Datenbank, in ein Archivsystem oder in ein anderes System 
überführt werden.  
Bei einer erforderlichen Datenumwandlung (Migration) darf ausschließlich das 
Format der Daten (z. B. Datums- und Währungsformat) umgesetzt, nicht aber 
eine inhaltliche Änderung der Daten vorgenommen werden. Die vorgenomme-
nen Änderungen sind zu dokumentieren.  
Die Reorganisation von OCR-Datenbanken ist zulässig, soweit die zugrunde 
liegenden elektronischen Dokumente und Unterlagen durch diesen Vorgang 
unverändert bleiben und die durch das OCR-Verfahren gewonnenen 
Informationen mindestens in quantitativer und qualitativer Hinsicht erhalten 
bleiben. 
1. Das neue System, das Archivsystem oder das andere System muss in quantitati-
ver und qualitativer Hinsicht die gleichen Auswertungen der aufzeichnungs- 
und aufbewahrungspflichtigen Daten ermöglichen als wären die Daten noch im 
Produktivsystem. 
 


--- Page 36 ---
 
Seite 36
143 
Andernfalls ist die ursprüngliche Hard- und Software des Produktivsystems - neben 
den aufzeichnungs- und aufbewahrungspflichtigen Daten - für die Dauer der Aufbe-
wahrungsfrist vorzuhalten. Auf die Möglichkeit der Bewilligung von Erleichterungen 
nach § 148 AO wird hingewiesen. 
144 
Eine Aufbewahrung in Form von Datenextrakten, Reports oder Druckdateien ist 
unzulässig, soweit nicht mehr alle aufzeichnungs- und aufbewahrungspflichtigen 
Daten übernommen werden. 
10. Nachvollziehbarkeit und Nachprüfbarkeit 
145 
Die allgemeinen Grundsätze der Nachvollziehbarkeit und Nachprüfbarkeit sind unter 
3.1 aufgeführt.  
Die Prüfbarkeit der formellen und sachlichen Richtigkeit bezieht sich sowohl auf 
einzelne Geschäftsvorfälle (Einzelprüfung) als auch auf die Prüfbarkeit des gesamten 
Verfahrens (Verfahrens- oder Systemprüfung anhand einer Verfahrensdokumentation, 
siehe unter 10.1).  
146 
Auch an die DV-gestützte Buchführung wird die Anforderung gestellt, dass Geschäfts-
vorfälle für die Dauer der Aufbewahrungsfrist retrograd und progressiv prüfbar 
bleiben müssen.  
147 
Die vorgenannten Anforderungen gelten für sonst erforderliche elektronische Auf-
zeichnungen sinngemäß (§ 145 Absatz 2 AO).  
148 
Von einem sachverständigen Dritten kann zwar Sachverstand hinsichtlich der 
Ordnungsvorschriften der §§ 145 bis 147 AO und allgemeiner DV-Sachverstand 
erwartet werden, nicht jedoch spezielle, produktabhängige System- oder Programmier-
kenntnisse.  
149 
Nach § 146 Absatz 3 Satz 3 AO muss im Einzelfall die Bedeutung von Abkürzungen, 
Ziffern, Buchstaben und Symbolen eindeutig festliegen und sich aus der Verfahrens-
dokumentation ergeben.  
150 
Für die Prüfung ist eine aussagefähige und aktuelle Verfahrensdokumentation 
notwendig, die alle System- bzw. Verfahrensänderungen inhaltlich und zeitlich 
lückenlos dokumentiert. 
10.1 
Verfahrensdokumentation 
151 
Da sich die Ordnungsmäßigkeit neben den elektronischen Büchern und sonst erforder-
lichen Aufzeichnungen auch auf die damit in Zusammenhang stehenden Verfahren 
und Bereiche des DV-Systems bezieht (siehe unter 3.), muss für jedes DV-System eine 
übersichtlich gegliederte Verfahrensdokumentation vorhanden sein, aus der Inhalt, 


--- Page 37 ---
 
Seite 37
Aufbau, Ablauf und Ergebnisse des DV-Verfahrens vollständig und schlüssig ersicht-
lich sind. Der Umfang der im Einzelfall erforderlichen Dokumentation wird dadurch 
bestimmt, was zum Verständnis des DV-Verfahrens, der Bücher und Aufzeichnungen 
sowie der aufbewahrten Unterlagen notwendig ist. Die Verfahrensdokumentation muss 
verständlich und damit für einen sachverständigen Dritten in angemessener Zeit nach-
prüfbar sein. Die konkrete Ausgestaltung der Verfahrensdokumentation ist abhängig 
von der Komplexität und Diversifikation der Geschäftstätigkeit und der Organisations-
struktur sowie des eingesetzten DV-Systems.  
152 
Die Verfahrensdokumentation beschreibt den organisatorisch und technisch gewollten 
Prozess, z. B. bei elektronischen Dokumenten von der Entstehung der Informationen 
über die Indizierung, Verarbeitung und Speicherung, dem eindeutigen Wiederfinden 
und der maschinellen Auswertbarkeit, der Absicherung gegen Verlust und Verfäl-
schung und der Reproduktion.  
153 
Die Verfahrensdokumentation besteht in der Regel aus einer allgemeinen Beschrei-
bung, einer Anwenderdokumentation, einer technischen Systemdokumentation und 
einer Betriebsdokumentation.  
154 
Für den Zeitraum der Aufbewahrungsfrist muss gewährleistet und nachgewiesen sein, 
dass das in der Dokumentation beschriebene Verfahren dem in der Praxis eingesetzten 
Verfahren voll entspricht. Dies gilt insbesondere für die eingesetzten Versionen der 
Programme (Programmidentität). Änderungen einer Verfahrensdokumentation müssen 
historisch nachvollziehbar sein. Dem wird genügt, wenn die Änderungen versioniert 
sind und eine nachvollziehbare Änderungshistorie vorgehalten wird. Aus der Verfah-
rensdokumentation muss sich ergeben, wie die Ordnungsvorschriften (z. B. §§ 145 ff. 
AO, §§ 238 ff. HGB) und damit die in diesem Schreiben enthaltenen Anforderungen 
beachtet werden. Die Aufbewahrungsfrist für die Verfahrensdokumentation läuft nicht 
ab, soweit und solange die Aufbewahrungsfrist für die Unterlagen noch nicht abgelaufen 
ist, zu deren Verständnis sie erforderlich ist.  
155 
Soweit eine fehlende oder ungenügende Verfahrensdokumentation die Nachvoll-
ziehbarkeit und Nachprüfbarkeit nicht beeinträchtigt, liegt kein formeller Mangel mit 
sachlichem Gewicht vor, der zum Verwerfen der Buchführung führen kann. 
10.2 
Lesbarmachung von elektronischen Unterlagen 
156 
Wer aufzubewahrende Unterlagen in der Form einer Wiedergabe auf einem Bildträger 
oder auf anderen Datenträgern vorlegt, ist nach § 147 Absatz 5 AO verpflichtet, auf 
seine Kosten diejenigen Hilfsmittel zur Verfügung zu stellen, die erforderlich sind, um 
die Unterlagen lesbar zu machen. Auf Verlangen der Finanzbehörde hat der Steuer-
pflichtige auf seine Kosten die Unterlagen unverzüglich ganz oder teilweise auszu-
drucken oder ohne Hilfsmittel lesbare Reproduktionen beizubringen. 


--- Page 38 ---
 
Seite 38
157 
Der Steuerpflichtige muss durch Erfassen im Sinne der Rz. 130 digitalisierte Unter-
lagen über sein DV-System per Bildschirm lesbar machen. Ein Ausdruck auf Papier ist 
nicht ausreichend. Die elektronischen Dokumente müssen für die Dauer der Aufbe-
wahrungsfrist jederzeit lesbar sein (BFH-Beschluss vom 26. September 2007, BStBl II 
2008 S. 415). 
11. Datenzugriff 
158 
Die Finanzbehörde hat das Recht, die mit Hilfe eines DV-Systems erstellten und nach 
§ 147 Absatz 1 AO aufbewahrungspflichtigen Unterlagen durch Datenzugriff zu 
prüfen. Das Recht auf Datenzugriff steht der Finanzbehörde nur im Rahmen der 
gesetzlichen Regelungen zu (z.B. Außenprüfung und Nachschauen). Durch die 
Regelungen zum Datenzugriff wird der sachliche Umfang der Außenprüfung (§ 194 
AO) nicht erweitert; er wird durch die Prüfungsanordnung (§ 196 AO, § 5 BpO) 
bestimmt.  
11.1 
Umfang und Ausübung des Rechts auf Datenzugriff nach § 147 Absatz 6 
AO 
159 
Gegenstand der Prüfung sind die nach außersteuerlichen und steuerlichen Vorschriften 
aufzeichnungspflichtigen und die nach § 147 Absatz 1 AO aufbewahrungspflichtigen 
Unterlagen. Hierfür sind insbesondere die Daten der Finanzbuchhaltung, der Anlagen-
buchhaltung, der Lohnbuchhaltung und aller Vor- und Nebensysteme, die aufzeich-
nungs- und aufbewahrungspflichtige Unterlagen enthalten (vgl. Rzn. 3 bis 5), für den 
Datenzugriff bereitzustellen. Die Art der Außenprüfung ist hierbei unerheblich, so 
dass z. B. die Daten der Finanzbuchhaltung auch Gegenstand der Lohnsteuer-Außen-
prüfung sein können. 
160 
Neben den Daten müssen insbesondere auch die Teile der Verfahrensdokumentation 
auf Verlangen zur Verfügung gestellt werden können, die einen vollständigen 
Systemüberblick ermöglichen und für das Verständnis des DV-Systems erforderlich 
sind. Dazu gehört auch ein Überblick über alle im DV-System vorhandenen Informa-
tionen, die aufzeichnungs- und aufbewahrungspflichtige Unterlagen betreffen (vgl. 
Rzn. 3 bis 5); z. B. Beschreibungen zu Tabellen, Feldern, Verknüpfungen und 
Auswertungen. Diese Angaben sind erforderlich, damit die Finanzverwaltung das 
durch den Steuerpflichtigen ausgeübte Erstqualifikationsrecht (vgl. Rz. 161) prüfen 
und Aufbereitungen für die Datenträgerüberlassung erstellen kann. 
161 
Soweit in Bereichen des Unternehmens betriebliche Abläufe mit Hilfe eines DV-
Systems abgebildet werden, sind die betroffenen DV-Systeme durch den Steuer-
pflichtigen zu identifizieren, die darin enthaltenen Daten nach Maßgabe der außer-
steuerlichen und steuerlichen Aufzeichnungs- und Aufbewahrungspflichten 


--- Page 39 ---
 
Seite 39
(vgl. Rzn. 3 bis 5) zu qualifizieren (Erstqualifizierung) und für den Datenzugriff in 
geeigneter Weise vorzuhalten (siehe auch unter 9.4). Bei unzutreffender Qualifi-
zierung von Daten kann die Finanzbehörde im Rahmen ihres pflichtgemäßen 
Ermessens verlangen, dass der Steuerpflichtige den Datenzugriff auf diese nach 
außersteuerlichen und steuerlichen Vorschriften tatsächlich aufgezeichneten und 
aufbewahrten Daten nachträglich ermöglicht.  
Beispiele 12: 
• Ein Steuerpflichtiger stellt aus dem PC-Kassensystem nur Tagesendsummen zur 
Verfügung. Die digitalen Grund(buch)aufzeichnungen (Kasseneinzeldaten) wur-
den archiviert, aber nicht zur Verfügung gestellt. 
• Ein Steuerpflichtiger stellt für die Datenträgerüberlassung nur einzelne Sachkonten 
aus der Finanzbuchhaltung zur Verfügung. Die Daten der Finanzbuchhaltung sind 
archiviert. 
• Ein Steuerpflichtiger ohne Auskunftsverweigerungsrecht stellt Belege in Papier-
form zur Verfügung. Die empfangenen und abgesandten Handels- und Geschäfts-
briefe und Buchungsbelege stehen in einem Dokumenten-Management-System zur 
Verfügung. 
162 
Das allgemeine Auskunftsrecht des Prüfers (§§ 88, 199 Absatz 1 AO) und die 
Mitwirkungspflichten des Steuerpflichtigen (§§ 90, 200 AO) bleiben unberührt. 
163 
Bei der Ausübung des Rechts auf Datenzugriff stehen der Finanzbehörde nach dem 
Gesetz drei gleichberechtigte Möglichkeiten zur Verfügung.  
164 
Die Entscheidung, von welcher Möglichkeit des Datenzugriffs die Finanzbehörde 
Gebrauch macht, steht in ihrem pflichtgemäßen Ermessen; falls erforderlich, kann sie 
auch kumulativ mehrere Möglichkeiten in Anspruch nehmen (Rzn. 165 bis 170). 
Sofern noch nicht mit der Außenprüfung begonnen wurde, ist es im Falle eines 
Systemwechsels oder einer Auslagerung von aufzeichnungs- und aufbewahrungs-
pflichtigen Daten aus dem Produktivsystem ausreichend, wenn nach Ablauf des 
5. Kalenderjahres, das auf die Umstellung folgt, nur noch der Z3-Zugriff (Rzn. 167 bis 
170) zur Verfügung gestellt wird. 
 
165 
Unmittelbarer Datenzugriff (Z1) 
Die Finanzbehörde hat das Recht, selbst unmittelbar auf das DV-System dergestalt 
zuzugreifen, dass sie in Form des Nur-Lesezugriffs Einsicht in die aufzeichnungs- und 
aufbewahrungspflichtigen Daten nimmt und die vom Steuerpflichtigen oder von einem 
beauftragten Dritten eingesetzte Hard- und Software zur Prüfung der gespeicherten 
Daten einschließlich der jeweiligen Meta-, Stamm- und Bewegungsdaten sowie der 
entsprechenden Verknüpfungen (z. B. zwischen den Tabellen einer relationalen 
Datenbank) nutzt.  


--- Page 40 ---
 
Seite 40
Dabei darf sie nur mit Hilfe dieser Hard- und Software auf die elektronisch gespei-
cherten Daten zugreifen. Dies schließt eine Fernabfrage (Online-Zugriff) der 
Finanzbehörde auf das DV-System des Steuerpflichtigen durch die Finanzbehörde aus. 
Der Nur-Lesezugriff umfasst das Lesen und Analysieren der Daten unter Nutzung der 
im DV-System vorhandenen Auswertungsmöglichkeiten (z. B. Filtern und Sortieren). 
166 
Mittelbarer Datenzugriff (Z2) 
Die Finanzbehörde kann vom Steuerpflichtigen auch verlangen, dass er an ihrer Stelle 
die aufzeichnungs- und aufbewahrungspflichtigen Daten nach ihren Vorgaben 
maschinell auswertet oder von einem beauftragten Dritten maschinell auswerten lässt, 
um anschließend einen Nur-Lesezugriff durchführen zu können. Es kann nur eine 
maschinelle Auswertung unter Verwendung der im DV-System des Steuerpflichtigen 
oder des beauftragten Dritten vorhandenen Auswertungsmöglichkeiten verlangt 
werden. 
167 
Datenträgerüberlassung (Z3) 
Die Finanzbehörde kann ferner verlangen, dass ihr die aufzeichnungs- und aufbewah-
rungspflichtigen Daten, einschließlich der jeweiligen Meta-, Stamm- und Bewegungs-
daten sowie der internen und externen Verknüpfungen (z. B. zwischen den Tabellen 
einer relationalen Datenbank), und elektronische Dokumente und Unterlagen auf 
einem maschinell lesbaren und auswertbaren Datenträger zur Auswertung überlassen 
werden. Die Finanzbehörde ist nicht berechtigt, selbst Daten aus dem DV-System 
herunterzuladen oder Kopien vorhandener Datensicherungen vorzunehmen. 
168 
Die Datenträgerüberlassung umfasst die Mitnahme der Daten aus der Sphäre des 
Steuerpflichtigen. Eine Mitnahme der Datenträger aus der Sphäre des Steuerpflich-
tigen sollte im Regelfall nur in Abstimmung mit dem Steuerpflichtigen erfolgen. 
169 
Der zur Auswertung überlassene Datenträger ist spätestens nach Bestandskraft der 
aufgrund der Außenprüfung ergangenen Bescheide an den Steuerpflichtigen zurück-
zugeben und die Daten sind zu löschen. 
170 
Die Finanzbehörde hat bei Anwendung der Regelungen zum Datenzugriff den Grund-
satz der Verhältnismäßigkeit zu beachten. 
11.2 
Umfang der Mitwirkungspflicht nach §§ 147 Absatz 6 und 200 Absatz 1 
Satz 2 AO 
171 
Der Steuerpflichtige hat die Finanzbehörde bei Ausübung ihres Rechts auf Datenzu-
griff zu unterstützen (§ 200 Absatz 1 AO). Dabei entstehende Kosten hat der Steuer-
pflichtige zu tragen (§ 147 Absatz 6 Satz 3 AO). 
172 
Enthalten elektronisch gespeicherte Datenbestände z. B. nicht aufzeichnungs- und auf-
bewahrungspflichtige, personenbezogene oder dem Berufsgeheimnis (§ 102 AO) 


--- Page 41 ---
 
Seite 41
unterliegende Daten, so obliegt es dem Steuerpflichtigen oder dem von ihm beauftrag-
ten Dritten, die Datenbestände so zu organisieren, dass der Prüfer nur auf die auf-
zeichnungs- und aufbewahrungspflichtigen Daten des Steuerpflichtigen zugreifen 
kann. Dies kann z. B. durch geeignete Zugriffsbeschränkungen oder „digitales 
Schwärzen“ der zu schützenden Informationen erfolgen. Für versehentlich überlassene 
Daten besteht kein Verwertungsverbot. 
173 
Mangels Nachprüfbarkeit akzeptiert die Finanzbehörde keine Reports oder Druck-
dateien, die vom Unternehmen ausgewählte („vorgefilterte“) Datenfelder und -sätze 
aufführen, jedoch nicht mehr alle aufzeichnungs- und aufbewahrungspflichtigen Daten 
(vgl. Rzn. 3 bis 5) enthalten.  
Im Einzelnen gilt Folgendes: 
174 
Beim unmittelbaren Datenzugriff hat der Steuerpflichtige dem Prüfer die für den 
Datenzugriff erforderlichen Hilfsmittel zur Verfügung zu stellen und ihn für den Nur-
Lesezugriff in das DV-System einzuweisen. Die Zugangsberechtigung muss so aus-
gestaltet sein, dass dem Prüfer dieser Zugriff auf alle aufzeichnungs- und aufbewah-
rungspflichtigen Daten eingeräumt wird. Sie umfasst die im DV-System genutzten 
Auswertungsmöglichkeiten (z. B. Filtern, Sortieren, Konsolidieren) für Prüfungs-
zwecke (z. B. in Revisionstools, Standardsoftware, Backofficeprodukten). In Abhän-
gigkeit vom konkreten Sachverhalt kann auch eine vom Steuerpflichtigen nicht 
genutzte, aber im DV-System vorhandene Auswertungsmöglichkeit verlangt werden.  
Eine Volltextsuche, eine Ansichtsfunktion oder ein selbsttragendes System, das in 
einer Datenbank nur die für archivierte Dateien vergebenen Schlagworte als Index-
werte nachweist, reicht regelmäßig nicht aus. 
Eine Unveränderbarkeit des Datenbestandes und des DV-Systems durch die Finanz-
behörde muss seitens des Steuerpflichtigen oder eines von ihm beauftragten Dritten 
gewährleistet werden. 
175 
Beim mittelbaren Datenzugriff gehört zur Mithilfe des Steuerpflichtigen beim Nur-
Lesezugriff neben der Zurverfügungstellung von Hard- und Software die Unter-
stützung durch mit dem DV-System vertraute Personen. Der Umfang der zumutbaren 
Mithilfe richtet sich nach den betrieblichen Gegebenheiten des Unternehmens.  
Hierfür können z. B. seine Größe oder Mitarbeiterzahl Anhaltspunkte sein. 
176 
Bei der Datenträgerüberlassung sind der Finanzbehörde mit den gespeicherten Unter-
lagen und Aufzeichnungen alle zur Auswertung der Daten notwendigen Informationen 
(z. B. über die Dateiherkunft [eingesetztes System], die Dateistruktur, die Datenfelder, 
verwendete Zeichensatztabellen sowie interne und externe Verknüpfungen) in 
maschinell auswertbarer Form zur Verfügung zu stellen. Dies gilt auch in den Fällen, 
in denen sich die Daten bei einem Dritten befinden. 
Auch die zur Auswertung der Daten notwendigen Strukturinformationen müssen in 


--- Page 42 ---
 
Seite 42
maschinell auswertbarer Form zur Verfügung gestellt werden. 
Bei unvollständigen oder unzutreffenden Datenlieferungen kann die Finanzbehörde 
neue Datenträger mit vollständigen und zutreffenden Daten verlangen. Im Verlauf der 
Prüfung kann die Finanzbehörde auch weitere Datenträger mit aufzeichnungs- und 
aufbewahrungspflichtigen Unterlagen anfordern. 
Das Einlesen der Daten muss ohne Installation von Fremdsoftware auf den Rechnern 
der Finanzbehörde möglich sein. Eine Entschlüsselung der übergebenen Daten muss 
spätestens bei der Datenübernahme auf die Systeme der Finanzverwaltung erfolgen. 
177 
Der Grundsatz der Wirtschaftlichkeit rechtfertigt nicht den Einsatz einer Software, die 
den in diesem Schreiben niedergelegten Anforderungen zur Datenträgerüberlassung 
nicht oder nur teilweise genügt und damit den Datenzugriff einschränkt. Die zur Her-
stellung des Datenzugriffs erforderlichen Kosten muss der Steuerpflichtige genauso in 
Kauf nehmen wie alle anderen Aufwendungen, die die Art seines Betriebes mit sich 
bringt. 
178 
Ergänzende Informationen zur Datenträgerüberlassung stehen auf der Internet-Seite 
des Bundesministeriums der Finanzen zum Download bereit. Die Digitale Schnittstelle 
der Finanzverwaltung für Kassensysteme (DSFinV-K) steht auf der Internet-Seite des 
Bundeszentralamts für Steuern (www.bzst.de) zum Download bereit. 
12. Zertifizierung und Software-Testate 
179 
Die Vielzahl und unterschiedliche Ausgestaltung und Kombination der DV-Systeme 
für die Erfüllung außersteuerlicher oder steuerlicher Aufzeichnungs- und Aufbewah-
rungspflichten lassen keine allgemein gültigen Aussagen der Finanzbehörde zur 
Konformität der verwendeten oder geplanten Hard- und Software zu. Dies gilt umso 
mehr, als weitere Kriterien (z. B. Releasewechsel, Updates, die Vergabe von Zugriffs-
rechten oder Parametrisierungen, die Vollständigkeit und Richtigkeit der eingegebenen 
Daten) erheblichen Einfluss auf die Ordnungsmäßigkeit eines DV-Systems und damit 
auf Bücher und die sonst erforderlichen Aufzeichnungen haben können. 
180 
Positivtestate zur Ordnungsmäßigkeit der Buchführung - und damit zur Ordnungs-
mäßigkeit DV-gestützter Buchführungssysteme - werden weder im Rahmen einer 
steuerlichen Außenprüfung noch im Rahmen einer verbindlichen Auskunft erteilt. 
181 
„Zertifikate“ oder „Testate“ Dritter können bei der Auswahl eines Softwareproduktes 
dem Unternehmen als Entscheidungskriterium dienen, entfalten jedoch aus den in 
Rz. 179 genannten Gründen gegenüber der Finanzbehörde keine Bindungswirkung. 
13. Anwendungsregelung 
182 
Im Übrigen bleiben die Regelungen des BMF-Schreibens vom 1. Februar 1984 
(IV A 7 - S 0318-1/84, BStBl I S. 155) unberührt. 


--- Page 43 ---
 
Seite 43
183 
Dieses BMF-Schreiben tritt mit Wirkung vom 1. Januar 2020 an die Stelle des BMF-
Schreibens vom 14. November 2014 - IV A 4 - S 0316/13/10003 -, BStBl I S. 1450.  
184 
Die übrigen Grundsätze dieses Schreibens sind auf Besteuerungszeiträume 
anzuwenden, die nach dem 31. Dezember 2019 beginnen. Es wird nicht beanstandet, 
wenn der Steuerpflichtige diese Grundsätze auf Besteuerungszeiträume anwendet, die 
vor dem 1. Januar 2020 enden. 
 
 
 


--- Page 44 ---
 
Seite 44
Dieses Schreiben wird im Bundessteuerblatt Teil I veröffentlicht. 
 
Im Auftrag 
 
', '99188dfdc452af6c927dba5ff05f9c7a62eefb1bcf5134d205759c6925a29065', NULL, '2026-01-16T16:59:02.275733+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (2, 'document', '/home/ghost/Repositories/Uni-Projekt-Graph-RAG/workspace/job_bad7b675-da31-439f-a32e-e9505c8ab308/documents/GoBD.pdf', 'GoBD.pdf', NULL, '--- Page 1 ---
 
Postanschrift Berlin: Bundesministeriu m der Finanzen, 11016 Berlin  
www.bundesfinanzministerium.de
 
 
 
 
POSTANSCHRIFT
Bundesministerium der Finanzen, 11016 Berlin 
 
Nur per E-Mail 
Oberste Finanzbehörden 
der Länder 
- bp@finmail.de - 
HAUSANSCHRIFT Wilhelmstraße 97 
10117 Berlin 
 
TEL +49 (0) 30 18 682-0 
 
 
 
E-MAIL poststelle@bmf.bund.de 
 
DATUM 28. November 2019 
 
 
 
BETREFF Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, 
Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff 
(GoBD) 
GZ IV A 4 - S 0316/19/10003 :001 
DOK 2019/0962810 
(bei Antwort bitte GZ und DOK angeben) 
 
Unter Bezugnahme auf das Ergebnis der Erörterungen mit den obersten Finanzbehörden der 
Länder gilt für die Anwendung dieser Grundsätze Folgendes: 
 
 


--- Page 2 ---
 
Seite 2
Inhalt 
1. 
ALLGEMEINES .......................................................................................................................................... 4 
1.1 
NUTZBARMACHUNG AUßERSTEUERLICHER BUCHFÜHRUNGS- UND AUFZEICHNUNGSPFLICHTEN FÜR DAS STEUERRECHT 4 
1.2 
STEUERLICHE BUCHFÜHRUNGS- UND AUFZEICHNUNGSPFLICHTEN ....................................................................... 4 
1.3 
AUFBEWAHRUNG VON UNTERLAGEN ZU GESCHÄFTSVORFÄLLEN UND VON SOLCHEN UNTERLAGEN, DIE ZUM 
VERSTÄNDNIS UND ZUR ÜBERPRÜFUNG DER FÜR DIE BESTEUERUNG GESETZLICH VORGESCHRIEBENEN 
AUFZEICHNUNGEN VON BEDEUTUNG SIND ...................................................................................................... 4 
1.4 
ORDNUNGSVORSCHRIFTEN ........................................................................................................................... 5 
1.5 
FÜHRUNG VON BÜCHERN UND SONST ERFORDERLICHEN AUFZEICHNUNGEN AUF DATENTRÄGERN ............................ 5 
1.6 
BEWEISKRAFT VON BUCHFÜHRUNG UND AUFZEICHNUNGEN, DARSTELLUNG VON BEANSTANDUNGEN DURCH DIE 
FINANZVERWALTUNG .................................................................................................................................. 6 
1.7 
AUFZEICHNUNGEN ...................................................................................................................................... 6 
1.8 
BÜCHER .................................................................................................................................................... 7 
1.9 
GESCHÄFTSVORFÄLLE .................................................................................................................................. 7 
1.10 
GRUNDSÄTZE ORDNUNGSMÄßIGER BUCHFÜHRUNG (GOB) ............................................................................... 7 
1.11 
DATENVERARBEITUNGSSYSTEM; HAUPT-, VOR- UND NEBENSYSTEME ................................................................. 8 
2. 
VERANTWORTLICHKEIT ........................................................................................................................... 8 
3. 
ALLGEMEINE ANFORDERUNGEN.............................................................................................................. 8 
3.1 
GRUNDSATZ DER NACHVOLLZIEHBARKEIT UND NACHPRÜFBARKEIT (§ 145 ABSATZ 1 AO, § 238 ABSATZ 1 SATZ 2 
UND SATZ 3 HGB) ................................................................................................................................... 10 
3.2 
GRUNDSÄTZE DER WAHRHEIT, KLARHEIT UND FORTLAUFENDEN AUFZEICHNUNG ................................................. 10 
3.2.1 
Vollständigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) ............................................................. 10 
3.2.2 
Richtigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) .................................................................... 12 
3.2.3 
Zeitgerechte Buchungen und Aufzeichnungen (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB)............ 12 
3.2.4 
Ordnung (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) ....................................................................... 14 
3.2.5 
Unveränderbarkeit (§ 146 Absatz 4 AO, § 239 Absatz 3 HGB) ....................................................... 15 
4. 
BELEGWESEN (BELEGFUNKTION) ............................................................................................................16 
4.1 
BELEGSICHERUNG ..................................................................................................................................... 17 
4.2 
ZUORDNUNG ZWISCHEN BELEG UND GRUND(BUCH)AUFZEICHNUNG ODER BUCHUNG .......................................... 17 
4.3 
ERFASSUNGSGERECHTE AUFBEREITUNG DER BUCHUNGSBELEGE ........................................................................ 18 
4.4 
BESONDERHEITEN ..................................................................................................................................... 21 
5. 
 AUFZEICHNUNG DER GESCHÄFTSVORFÄLLE IN ZEITLICHER REIHENFOLGE UND IN SACHLICHER 
ORDNUNG (GRUND(BUCH)AUFZEICHNUNGEN, JOURNAL- UND KONTENFUNKTION) .............................21 
5.1 
ERFASSUNG IN GRUND(BUCH)AUFZEICHNUNGEN ........................................................................................... 22 
5.2 
DIGITALE GRUND(BUCH)AUFZEICHNUNGEN................................................................................................... 22 
5.3 
VERBUCHUNG IM JOURNAL (JOURNALFUNKTION) .......................................................................................... 23 
5.4 
AUFZEICHNUNG DER GESCHÄFTSVORFÄLLE IN SACHLICHER ORDNUNG (HAUPTBUCH) ........................................... 24 
6. 
INTERNES KONTROLLSYSTEM (IKS) .........................................................................................................25 
7. 
DATENSICHERHEIT ..................................................................................................................................26 
8. 
UNVERÄNDERBARKEIT, PROTOKOLLIERUNG VON ÄNDERUNGEN ..........................................................26 


--- Page 3 ---
 
Seite 3
9.  
     AUFBEWAHRUNG ..............................................................................................................................28 
9.1 
MASCHINELLE AUSWERTBARKEIT (§ 147 ABSATZ 2 NUMMER 2 AO) ............................................................... 30 
9.2 
ELEKTRONISCHE AUFBEWAHRUNG ............................................................................................................... 31 
9.3 
BILDLICHE ERFASSUNG VON PAPIERDOKUMENTEN ......................................................................................... 33 
9.4 
AUSLAGERUNG VON DATEN AUS DEM PRODUKTIVSYSTEM UND SYSTEMWECHSEL ................................................ 34 
10. 
NACHVOLLZIEHBARKEIT UND NACHPRÜFBARKEIT .............................................................................35 
10.1 
VERFAHRENSDOKUMENTATION ................................................................................................................... 36 
10.2 
LESBARMACHUNG VON ELEKTRONISCHEN UNTERLAGEN .................................................................................. 37 
11. 
DATENZUGRIFF ...................................................................................................................................37 
11.1 
UMFANG UND AUSÜBUNG DES RECHTS AUF DATENZUGRIFF NACH § 147 ABSATZ 6 AO ...................................... 38 
11.2 
UMFANG DER MITWIRKUNGSPFLICHT NACH §§ 147 ABSATZ 6 UND 200 ABSATZ 1 SATZ 2 AO ............................ 40 
12. 
ZERTIFIZIERUNG UND SOFTWARE-TESTATE ........................................................................................42 
13. 
ANWENDUNGSREGELUNG .................................................................................................................42 
 
 
 


--- Page 4 ---
 
Seite 4
1. Allgemeines 
1 
 Die betrieblichen Abläufe in den Unternehmen werden ganz oder teilweise unter Ein-
satz von Informations- und Kommunikations-Technik abgebildet. 
2 
Auch die nach außersteuerlichen oder steuerlichen Vorschriften zu führenden Bücher 
und sonst erforderlichen Aufzeichnungen werden in den Unternehmen zunehmend in 
elektronischer Form geführt (z. B. als Datensätze). Darüber hinaus werden in den 
Unternehmen zunehmend die aufbewahrungspflichtigen Unterlagen in elektronischer 
Form (z. B. als elektronische Dokumente) aufbewahrt. 
1.1 Nutzbarmachung außersteuerlicher Buchführungs- und Aufzeichnungs-
pflichten für das Steuerrecht 
3 
Nach § 140 AO sind die außersteuerlichen Buchführungs- und Aufzeichnungspflich-
ten, die für die Besteuerung von Bedeutung sind, auch für das Steuerrecht zu erfüllen. 
Außersteuerliche Buchführungs- und Aufzeichnungspflichten ergeben sich insbeson-
dere aus den Vorschriften der §§ 238 ff. HGB und aus den dort bezeichneten handels-
rechtlichen Grundsätzen ordnungsmäßiger Buchführung (GoB). Für einzelne Rechts-
formen ergeben sich flankierende Aufzeichnungspflichten z. B. aus §§ 91 ff. Aktien-
gesetz, §§ 41 ff. GmbH-Gesetz oder § 33 Genossenschaftsgesetz. Des Weiteren sind 
zahlreiche gewerberechtliche oder branchenspezifische Aufzeichnungsvorschriften 
vorhanden, die gem. § 140 AO im konkreten Einzelfall für die Besteuerung von 
Bedeutung sind, wie z. B. Apothekenbetriebsordnung, Eichordnung, Fahrlehrergesetz, 
Gewerbeordnung, § 26 Kreditwesengesetz oder § 55 Versicherungsaufsichtsgesetz.  
1.2 Steuerliche Buchführungs- und Aufzeichnungspflichten 
4 
 Steuerliche Buchführungs- und Aufzeichnungspflichten ergeben sich sowohl aus der 
Abgabenordnung (z. B. §§ 90 Absatz 3, 141 bis 144 AO) als auch aus Einzelsteuer-
gesetzen (z. B. § 22 UStG, § 4 Absatz 3 Satz 5, § 4 Absatz 4a Satz 6, § 4 Absatz 7 und 
§ 41 EStG). 
1.3 Aufbewahrung von Unterlagen zu Geschäftsvorfällen und von solchen 
Unterlagen, die zum Verständnis und zur Überprüfung der für die Besteue-
rung gesetzlich vorgeschriebenen Aufzeichnungen von Bedeutung sind 
5 
Neben den außersteuerlichen und steuerlichen Büchern, Aufzeichnungen und Unter-
lagen zu Geschäftsvorfällen sind alle Unterlagen aufzubewahren, die zum Verständnis 
und zur Überprüfung der für die Besteuerung gesetzlich vorgeschriebenen Aufzeich-
nungen im Einzelfall von Bedeutung sind (vgl. BFH-Urteil vom 24. Juni 2009, 


--- Page 5 ---
 
Seite 5
BStBl II 2010 S. 452).  
 
Dazu zählen neben Unterlagen in Papierform auch alle Unterlagen in Form von Daten, 
Datensätzen und elektronischen Dokumenten, die dokumentieren, dass die Ordnungs-
vorschriften umgesetzt und deren Einhaltung überwacht wurde. Nicht aufbewahrungs-
pflichtig sind z. B. reine Entwürfe von Handels- oder Geschäftsbriefen, sofern diese 
nicht tatsächlich abgesandt wurden. 
 
Beispiel 1: 
Dienen Kostenstellen der Bewertung von Wirtschaftsgütern, von Rückstellungen oder 
als Grundlage für die Bemessung von Verrechnungspreisen sind diese Aufzeichnun-
gen aufzubewahren, soweit sie zur Erläuterung steuerlicher Sachverhalte benötigt 
werden. 
 
6 
Form, Umfang und Inhalt dieser im Sinne der Rzn. 3 bis 5 nach außersteuerlichen und 
steuerlichen Rechtsnormen aufzeichnungs- und aufbewahrungspflichtigen Unterlagen 
(Daten, Datensätze sowie Dokumente in elektronischer oder Papierform) und der zu 
ihrem Verständnis erforderlichen Unterlagen werden durch den Steuerpflichtigen 
bestimmt. Eine abschließende Definition der aufzeichnungs- und aufbewahrungs-
pflichtigen Aufzeichnungen und Unterlagen ist nicht Gegenstand der nachfolgenden 
Ausführungen. Die Finanzverwaltung kann diese Unterlagen nicht abstrakt im Vorfeld 
für alle Unternehmen abschließend definieren, weil die betrieblichen Abläufe, die auf-
zeichnungs- und aufbewahrungspflichtigen Aufzeichnungen und Unterlagen sowie die 
eingesetzten Buchführungs- und Aufzeichnungssysteme in den Unternehmen zu unter-
schiedlich sind. 
1.4 Ordnungsvorschriften 
7 
Die Ordnungsvorschriften der §§ 145 bis 147 AO gelten für die vorbezeichneten 
Bücher und sonst erforderlichen Aufzeichnungen und der zu ihrem Verständnis 
erforderlichen Unterlagen (vgl. Rzn. 3 bis 5; siehe auch Rzn. 23, 25 und 28). 
1.5 Führung von Büchern und sonst erforderlichen Aufzeichnungen auf 
Datenträgern 
8 
 Bücher und die sonst erforderlichen Aufzeichnungen können nach § 146 Absatz 5 AO 
auch auf Datenträgern geführt werden, soweit diese Form der Buchführung einschließ-
lich des dabei angewandten Verfahrens den GoB entspricht (siehe unter 1.4.). Bei Auf-
zeichnungen, die allein nach den Steuergesetzen vorzunehmen sind, bestimmt sich die 
Zulässigkeit des angewendeten Verfahrens nach dem Zweck, den die Aufzeichnungen 
für die Besteuerung erfüllen sollen (§ 145 Absatz 2 AO; § 146 Absatz 5 Satz 1 2. HS 


--- Page 6 ---
 
Seite 6
AO). Unter diesen Voraussetzungen sind auch Aufzeichnungen auf Datenträgern 
zulässig. 
9 
Somit sind alle Unternehmensbereiche betroffen, in denen betriebliche Abläufe durch 
DV-gestützte Verfahren abgebildet werden und ein Datenverarbeitungssystem (DV-
System, siehe auch Rz. 20) für die Erfüllung der in den Rzn. 3 bis 5 bezeichneten 
außersteuerlichen oder steuerlichen Buchführungs-, Aufzeichnungs- und Aufbewah-
rungspflichten verwendet wird (siehe auch unter 11.1 zum Datenzugriffsrecht). 
10 
Technische Vorgaben oder Standards (z. B. zu Archivierungsmedien oder Kryptogra-
fieverfahren) können angesichts der rasch fortschreitenden Entwicklung und der eben-
falls notwendigen Betrachtung des organisatorischen Umfelds nicht festgelegt werden. 
Im Zweifel ist über einen Analogieschluss festzustellen, ob die Ordnungsvorschriften 
eingehalten wurden, z. B. bei einem Vergleich zwischen handschriftlich geführten 
Handelsbüchern und Unterlagen in Papierform, die in einem verschlossenen Schrank 
aufbewahrt werden, einerseits und elektronischen Handelsbüchern und Unterlagen, die 
mit einem elektronischen Zugriffsschutz gespeichert werden, andererseits. 
1.6 Beweiskraft von Buchführung und Aufzeichnungen, Darstellung von 
Beanstandungen durch die Finanzverwaltung 
11 
Nach § 158 AO sind die Buchführung und die Aufzeichnungen des Steuerpflichtigen, 
die den Vorschriften der §§ 140 bis 148 AO entsprechen, der Besteuerung zugrunde zu 
legen, soweit nach den Umständen des Einzelfalls kein Anlass besteht, ihre sachliche 
Richtigkeit zu beanstanden. Werden Buchführung oder Aufzeichnungen des Steuer-
pflichtigen im Einzelfall durch die Finanzverwaltung beanstandet, so ist durch die 
Finanzverwaltung der Grund der Beanstandung in geeigneter Form darzustellen. 
1.7 Aufzeichnungen 
12 
Aufzeichnungen sind alle dauerhaft verkörperten Erklärungen über Geschäftsvorfälle 
in Schriftform oder auf Medien mit Schriftersatzfunktion (z. B. auf Datenträgern).  
Der Begriff der Aufzeichnungen umfasst Darstellungen in Worten, Zahlen, Symbolen 
und Grafiken.  
13 
Werden Aufzeichnungen nach verschiedenen Rechtsnormen in einer Aufzeichnung 
zusammengefasst (z. B. nach §§ 238 ff. HGB und nach § 22 UStG), müssen die 
zusammengefassten Aufzeichnungen den unterschiedlichen Zwecken genügen. 
Erfordern verschiedene Rechtsnormen gleichartige Aufzeichnungen, so ist eine 
mehrfache Aufzeichnung für jede Rechtsnorm nicht erforderlich. 


--- Page 7 ---
 
Seite 7
1.8 Bücher 
14 
Der Begriff ist funktional unter Anknüpfung an die handelsrechtliche Bedeutung zu 
verstehen. Die äußere Gestalt (gebundenes Buch, Loseblattsammlung oder 
Datenträger) ist unerheblich.  
15 
Der Kaufmann ist verpflichtet, in den Büchern seine Handelsgeschäfte und die Lage 
des Vermögens ersichtlich zu machen (§ 238 Absatz 1 Satz 1 HGB). Der Begriff 
Bücher umfasst sowohl die Handelsbücher der Kaufleute (§§ 238 ff. HGB) als auch 
die diesen entsprechenden Aufzeichnungen von Geschäftsvorfällen der Nichtkauf-
leute. Bei Kleinstunternehmen, die ihren Gewinn durch Einnahmen-Überschussrech-
nung ermitteln (bis 17.500 Euro Jahresumsatz), ist die Erfüllung der Anforderungen an 
die Aufzeichnungen nach den GoBD regelmäßig auch mit Blick auf die Unterneh-
mensgröße zu bewerten. 
1.9 Geschäftsvorfälle 
16 
Geschäftsvorfälle sind alle rechtlichen und wirtschaftlichen Vorgänge, die innerhalb 
eines bestimmten Zeitabschnitts den Gewinn bzw. Verlust oder die Vermögenszusam-
mensetzung in einem Unternehmen dokumentieren oder beeinflussen bzw. verändern 
(z. B. zu einer Veränderung des Anlage- und Umlaufvermögens sowie des Eigen- und 
Fremdkapitals führen). 
1.10 
Grundsätze ordnungsmäßiger Buchführung (GoB) 
17 
Die GoB sind ein unbestimmter Rechtsbegriff, der insbesondere durch Rechtsnormen 
und Rechtsprechung geprägt ist und von der Rechtsprechung und Verwaltung jeweils 
im Einzelnen auszulegen und anzuwenden ist (BFH-Urteil vom 12. Mai 1966, BStBl III 
S. 371; BVerfG-Beschluss vom 10. Oktober 1961, 2 BvL 1/59, BVerfGE 13 S. 153). 
 
18 
Die GoB können sich durch gutachterliche Stellungnahmen, Handelsbrauch, ständige 
Übung, Gewohnheitsrecht, organisatorische und technische Änderungen weiterent-
wickeln und sind einem Wandel unterworfen. 
 
19 
Die GoB enthalten sowohl formelle als auch materielle Anforderungen an eine Buch-
führung. Die formellen Anforderungen ergeben sich insbesondere aus den §§ 238 ff. 
HGB für Kaufleute und aus den §§ 145 bis 147 AO für Buchführungs- und Aufzeich-
nungspflichtige (siehe unter 3.). Materiell ordnungsmäßig sind Bücher und Aufzeich-
nungen, wenn die Geschäftsvorfälle einzeln, nachvollziehbar, vollständig, richtig, zeit-
gerecht und geordnet in ihrer Auswirkung erfasst und anschließend gebucht bzw. ver-
arbeitet sind (vgl. § 239 Absatz 2 HGB, § 145 AO, § 146 Absatz 1 AO). Siehe Rz. 11 
zur Beweiskraft von Buchführung und Aufzeichnungen. 
 


--- Page 8 ---
 
Seite 8
1.11 
Datenverarbeitungssystem; Haupt-, Vor- und Nebensysteme 
20 
Unter DV-System wird die im Unternehmen oder für Unternehmenszwecke zur elek-
tronischen Datenverarbeitung eingesetzte Hard- und Software verstanden, mit denen 
Daten und Dokumente im Sinne der Rzn. 3 bis 5 erfasst, erzeugt, empfangen, über-
nommen, verarbeitet, gespeichert oder übermittelt werden. Dazu gehören das Haupt-
system sowie Vor- und Nebensysteme (z. B. Finanzbuchführungssystem, Anlagen-
buchhaltung, Lohnbuchhaltungssystem, Kassensystem, Warenwirtschaftssystem, 
Zahlungsverkehrssystem, Taxameter, Geldspielgeräte, elektronische Waagen, 
Materialwirtschaft, Fakturierung, Zeiterfassung, Archivsystem, Dokumenten-
Management-System) einschließlich der Schnittstellen zwischen den Systemen. Auf 
die Bezeichnung des DV-Systems oder auf dessen Größe (z. B. Einsatz von Einzel-
geräten oder von Netzwerken) kommt es dabei nicht an. Ebenfalls kommt es nicht 
darauf an, ob die betreffenden DV-Systeme vom Steuerpflichtigen als eigene 
Hardware bzw. Software erworben und genutzt oder in einer Cloud bzw. als eine 
Kombination dieser Systeme betrieben werden. 
2. Verantwortlichkeit 
21 
Für die Ordnungsmäßigkeit elektronischer Bücher und sonst erforderlicher elektroni-
scher Aufzeichnungen im Sinne der Rzn. 3 bis 5, einschließlich der eingesetzten 
Verfahren, ist allein der Steuerpflichtige verantwortlich. Dies gilt auch bei einer 
teilweisen oder vollständigen organisatorischen und technischen Auslagerung von 
Buchführungs- und Aufzeichnungsaufgaben auf Dritte (z. B. Steuerberater oder 
Rechenzentrum).  
3. Allgemeine Anforderungen 
22 
Die Ordnungsmäßigkeit elektronischer Bücher und sonst erforderlicher elektronischer 
Aufzeichnungen im Sinne der Rzn. 3 bis 5 ist nach den gleichen Prinzipien zu beur-
teilen wie die Ordnungsmäßigkeit bei manuell erstellten Büchern oder Aufzeichnun-
gen. 
23 
Das Erfordernis der Ordnungsmäßigkeit erstreckt sich - neben den elektronischen 
Büchern und sonst erforderlichen Aufzeichnungen - auch auf die damit in Zusammen-
hang stehenden Verfahren und Bereiche des DV-Systems (siehe unter 1.11), da die 
Grundlage für die Ordnungsmäßigkeit elektronischer Bücher und sonst erforderlicher 
Aufzeichnungen bereits bei der Entwicklung und Freigabe von Haupt-, Vor- und 
Nebensystemen einschließlich des dabei angewandten DV-gestützten Verfahrens 
gelegt wird. Die Ordnungsmäßigkeit muss bei der Einrichtung und unternehmens-
spezifischen Anpassung des DV-Systems bzw. der DV-gestützten Verfahren im 


--- Page 9 ---
 
Seite 9
konkreten Unternehmensumfeld und für die Dauer der Aufbewahrungsfrist erhalten 
bleiben. 
24 
Die Anforderungen an die Ordnungsmäßigkeit ergeben sich aus: 
• außersteuerlichen Rechtsnormen (z. B. den handelsrechtlichen GoB gem. §§ 238, 
239, 257, 261 HGB), die gem. § 140 AO für das Steuerrecht nutzbar gemacht 
werden können, wenn sie für die Besteuerung von Bedeutung sind, und 
• steuerlichen Ordnungsvorschriften (insbesondere gem. §§ 145 bis 147 AO). 
25 
Die allgemeinen Ordnungsvorschriften in den §§ 145 bis 147 AO gelten nicht nur für 
Buchführungs- und Aufzeichnungspflichten nach § 140 AO und nach den §§ 141 
bis 144 AO. Insbesondere § 145 Absatz 2 AO betrifft alle zu Besteuerungszwecken 
gesetzlich geforderten Aufzeichnungen, also auch solche, zu denen der Steuer-
pflichtige aufgrund anderer Steuergesetze verpflichtet ist, wie z. B. nach § 4 Absatz 3 
Satz 5, Absatz 7 EStG und nach § 22 UStG (BFH-Urteil vom 24. Juni 2009, 
BStBl II 2010 S. 452). 
 
26 
Demnach sind bei der Führung von Büchern in elektronischer oder in Papierform und 
sonst erforderlicher Aufzeichnungen in elektronischer oder in Papierform im Sinne der 
Rzn. 3 bis 5 die folgenden Anforderungen zu beachten: 
• Grundsatz der Nachvollziehbarkeit und Nachprüfbarkeit (siehe unter 3.1), 
• Grundsätze der Wahrheit, Klarheit und fortlaufenden Aufzeichnung (siehe 
unter 3.2): 
 Vollständigkeit (siehe unter 3.2.1), 
 Einzelaufzeichnungspflicht (siehe unter 3.2.1), 
 Richtigkeit (siehe unter 3.2.2), 
 zeitgerechte Buchungen und Aufzeichnungen (siehe unter 3.2.3), 
 Ordnung (siehe unter 3.2.4), 
 Unveränderbarkeit (siehe unter 3.2.5). 
 
27 
Diese Grundsätze müssen während der Dauer der Aufbewahrungsfrist nachweisbar 
erfüllt werden und erhalten bleiben. 
28 
Nach § 146 Absatz 6 AO gelten die Ordnungsvorschriften auch dann, wenn der Unter-
nehmer elektronische Bücher und Aufzeichnungen führt, die für die Besteuerung von 
Bedeutung sind, ohne hierzu verpflichtet zu sein. 
 
29 
Der Grundsatz der Wirtschaftlichkeit rechtfertigt es nicht, dass Grundprinzipien der 
Ordnungsmäßigkeit verletzt und die Zwecke der Buchführung erheblich gefährdet 
werden. Die zur Vermeidung einer solchen Gefährdung erforderlichen Kosten muss 
der Steuerpflichtige genauso in Kauf nehmen wie alle anderen Aufwendungen, die die 
Art seines Betriebes mit sich bringt (BFH-Urteil vom 26. März 1968, BStBl II S. 527). 


--- Page 10 ---
 
Seite 10
3.1 Grundsatz der Nachvollziehbarkeit und Nachprüfbarkeit (§ 145 Absatz 1 
AO, § 238 Absatz 1 Satz 2 und Satz 3 HGB) 
30 
Die Verarbeitung der einzelnen Geschäftsvorfälle sowie das dabei angewandte Buch-
führungs- oder Aufzeichnungsverfahren müssen nachvollziehbar sein. Die Buchungen 
und die sonst erforderlichen Aufzeichnungen müssen durch einen Beleg nachgewiesen 
sein oder nachgewiesen werden können (Belegprinzip, siehe auch unter 4.).  
31 
Aufzeichnungen sind so vorzunehmen, dass der Zweck, den sie für die Besteuerung 
erfüllen sollen, erreicht wird. Damit gelten die nachfolgenden Anforderungen der 
progressiven und retrograden Prüfbarkeit - soweit anwendbar - sinngemäß. 
32 
Die Buchführung muss so beschaffen sein, dass sie einem sachverständigen Dritten 
innerhalb angemessener Zeit einen Überblick über die Geschäftsvorfälle und über die 
Lage des Unternehmens vermitteln kann. Die einzelnen Geschäftsvorfälle müssen sich 
in ihrer Entstehung und Abwicklung lückenlos verfolgen lassen (progressive und 
retrograde Prüfbarkeit). 
 
33 
Die progressive Prüfung beginnt beim Beleg, geht über die Grund(buch)aufzeich-
nungen und Journale zu den Konten, danach zur Bilanz mit Gewinn- und Verlust-
rechnung und schließlich zur Steueranmeldung bzw. Steuererklärung. Die retrograde 
Prüfung verläuft umgekehrt. Die progressive und retrograde Prüfung muss für die 
gesamte Dauer der Aufbewahrungsfrist und in jedem Verfahrensschritt möglich sein.  
34 
Die Nachprüfbarkeit der Bücher und sonst erforderlichen Aufzeichnungen erfordert 
eine aussagekräftige und vollständige Verfahrensdokumentation (siehe unter 10.1), die 
sowohl die aktuellen als auch die historischen Verfahrensinhalte für die Dauer der 
Aufbewahrungsfrist nachweist und den in der Praxis eingesetzten Versionen des DV-
Systems entspricht. 
 
35 
Die Nachvollziehbarkeit und Nachprüfbarkeit muss für die Dauer der Aufbewahrungs-
frist gegeben sein. Dies gilt auch für die zum Verständnis der Buchführung oder Auf-
zeichnungen erforderliche Verfahrensdokumentation. 
3.2 Grundsätze der Wahrheit, Klarheit und fortlaufenden Aufzeichnung 
3.2.1 Vollständigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) 
36 
Die Geschäftsvorfälle sind vollzählig und lückenlos aufzuzeichnen (Grundsatz der 
Einzelaufzeichnungspflicht; vgl. AEAO zu § 146 AO Nr. 2.1). Eine vollzählige und 
lückenlose Aufzeichnung von Geschäftsvorfällen ist auch dann gegeben, wenn 
zulässigerweise nicht alle Datenfelder eines Datensatzes gefüllt werden.  
37 
Die GoB erfordern in der Regel die Aufzeichnung jedes Geschäftsvorfalls - also auch 
jeder Betriebseinnahme und Betriebsausgabe, jeder Einlage und Entnahme - in einem 


--- Page 11 ---
 
Seite 11
Umfang, der eine Überprüfung seiner Grundlagen, seines Inhalts und seiner Bedeu-
tung für den Betrieb ermöglicht. Das bedeutet nicht nur die Aufzeichnung der in Geld 
bestehenden Gegenleistung, sondern auch des Inhalts des Geschäfts und des Namens 
des Vertragspartners (BFH-Urteil vom 12. Mai 1966, BStBl III S. 371) - soweit 
zumutbar, mit ausreichender Bezeichnung des Geschäftsvorfalls (BFH-Urteil vom 
1. Oktober 1969, BStBl 1970 II S. 45). Branchenspezifische Mindestaufzeichnungs-
pflichten und Zumutbarkeitsgesichtspunkte sind zu berücksichtigen. 
Beispiele 2 zu branchenspezifisch entbehrlichen Aufzeichnungen und zur 
Zumutbarkeit: 
• In einem Einzelhandelsgeschäft kommt zulässigerweise eine PC-Kasse ohne Kun-
denverwaltung zum Einsatz. Die Namen der Kunden werden bei Bargeschäften 
nicht erfasst und nicht beigestellt. - Keine Beanstandung. 
• Bei einem Taxiunternehmer werden Angaben zum Kunden im Taxameter nicht 
erfasst und nicht beigestellt. - Keine Beanstandung. 
 
38 
Dies gilt auch für Bareinnahmen; der Umstand der sofortigen Bezahlung rechtfertigt 
keine Ausnahme von diesem Grundsatz (BFH-Urteil vom 26. Februar 2004, BStBl II 
S. 599). 
39 
Die Aufzeichnung jedes einzelnen Geschäftsvorfalls ist nur dann nicht zumutbar, 
wenn es technisch, betriebswirtschaftlich und praktisch unmöglich ist, die einzelnen 
Geschäftsvorfälle aufzuzeichnen (BFH-Urteil vom 12. Mai 1966, IV 472/60, BStBl III 
S. 371). Das Vorliegen dieser Voraussetzungen ist durch den Steuerpflichtigen nach-
zuweisen. 
Beim Verkauf von Waren an eine Vielzahl von nicht bekannten Personen gegen 
Barzahlung gilt die Einzelaufzeichnungspflicht nach § 146 Absatz 1 Satz 1 AO aus 
Zumutbarkeitsgrü nden nicht, wenn kein elektronisches Aufzeichnungssystem, sondern 
eine offene Ladenkasse verwendet wird (§ 146 Absatz 1 Satz 3 und 4 AO, vgl. AEAO 
zu § 146, Nr. 2.1.4). Wird hingegen ein elektronisches Aufzeichnungssystem ver-
wendet, gilt die Einzelaufzeichnungspflicht nach § 146 Absatz 1 Satz 1 AO unab-
hängig davon, ob das elektronische Aufzeichnungssystem und die digitalen Aufzeich-
nungen nach § 146a Absatz 3 AO i. V. m. der KassenSichV mit einer zertifizierten 
technischen Sicherheitseinrichtung zu schü tzen sind. Die Zumutbarkeitsü berlegungen, 
die der Ausnahmeregelung nach § 146 Absatz 1 Satz 3 AO zugrunde liegen, sind 
grundsätzlich auch auf Dienstleistungen ü bertragbar (vgl. AEAO zu § 146, Nr. 2.2.6). 
40 
Die vollständige und lückenlose Erfassung und Wiedergabe aller Geschäftsvorfälle ist 
bei DV-Systemen durch ein Zusammenspiel von technischen (einschließlich program-
mierten) und organisatorischen Kontrollen sicherzustellen (z. B. Erfassungskontrollen, 


--- Page 12 ---
 
Seite 12
Plausibilitätskontrollen bei Dateneingaben, inhaltliche Plausibilitätskontrollen, auto-
matisierte Vergabe von Datensatznummern, Lückenanalyse oder Mehrfachbelegungs-
analyse bei Belegnummern).  
41 
Ein und derselbe Geschäftsvorfall darf nicht mehrfach aufgezeichnet werden. 
Beispiel 3: 
Ein Wareneinkauf wird gewinnwirksam durch Erfassung des zeitgleichen Liefer-
scheins und später nochmals mittels Erfassung der (Sammel)Rechnung erfasst und 
verbucht. Keine mehrfache Aufzeichnung eines Geschäftsvorfalles in verschiedenen 
Systemen oder mit verschiedenen Kennungen (z. B. für Handelsbilanz, für steuerliche 
Zwecke) liegt vor, soweit keine mehrfache bilanzielle oder gewinnwirksame Auswir-
kung gegeben ist.  
42 
Zusammengefasste oder verdichtete Aufzeichnungen im Hauptbuch (Konto) sind 
zulässig, sofern sie nachvollziehbar in ihre Einzelpositionen in den Grund(buch)auf-
zeichnungen oder des Journals aufgegliedert werden können. Andernfalls ist die 
Nachvollziehbarkeit und Nachprüfbarkeit nicht gewährleistet.  
43 
Die Erfassung oder Verarbeitung von tatsächlichen Geschäftsvorfällen darf nicht 
unterdrückt werden. So ist z. B. eine Bon- oder Rechnungserteilung ohne Registrie-
rung der bar vereinnahmten Beträge (Abbruch des Vorgangs) in einem DV-System 
unzulässig. 
3.2.2 Richtigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) 
44 
Geschäftsvorfälle sind in Übereinstimmung mit den tatsächlichen Verhältnissen und 
im Einklang mit den rechtlichen Vorschriften inhaltlich zutreffend durch Belege 
abzubilden (BFH-Urteil vom 24. Juni 1997, BStBl II 1998 S. 51), der Wahrheit ent-
sprechend aufzuzeichnen und bei kontenmäßiger Abbildung zutreffend zu kontieren.  
 
3.2.3 Zeitgerechte Buchungen und Aufzeichnungen (§ 146 Absatz 1 AO, § 239 
Absatz 2 HGB) 
45 
Das Erfordernis „zeitgerecht“ zu buchen verlangt, dass ein zeitlicher Zusammenhang 
zwischen den Vorgängen und ihrer buchmäßigen Erfassung besteht (BFH-Urteil vom 
25. März 1992, BStBl II S. 1010; BFH-Urteil vom 5. März 1965, BStBl III S. 285). 
46 
Jeder Geschäftsvorfall ist zeitnah, d. h. möglichst unmittelbar nach seiner Entstehung in 
einer Grundaufzeichnung oder in einem Grundbuch zu erfassen. Nach den GoB müssen 
die Geschäftsvorfälle grundsätzlich laufend gebucht werden (Journal). Es widerspricht 
dem Wesen der kaufmännischen Buchführung, sich zunächst auf die Sammlung von 
Belegen zu beschränken und nach Ablauf einer langen Zeit auf Grund dieser Belege die 
Geschäftsvorfälle in Grundaufzeichnungen oder Grundbüchern einzutragen (vgl. BFH-


--- Page 13 ---
 
Seite 13
Urteil vom 10. Juni 1954, BStBl III S. 298). Die Funktion der Grund(buch)aufzeich-
nungen kann auf Dauer auch durch eine geordnete und übersichtliche Belegablage 
erfüllt werden (§ 239 Absatz 4 HGB; § 146 Absatz 5 AO; H 5.2 „Grundbuchaufzeich-
nungen“ EStH). 
47 
Jede nicht durch die Verhältnisse des Betriebs oder des Geschäftsvorfalls zwingend 
bedingte Zeitspanne zwischen dem Eintritt des Vorganges und seiner laufenden 
Erfassung in Grund(buch)aufzeichnungen ist bedenklich. Eine Erfassung von unbaren 
Geschäftsvorfällen innerhalb von zehn Tagen ist unbedenklich. Wegen der Forderung 
nach zeitnaher chronologischer Erfassung der Geschäftsvorfälle ist zu verhindern, dass 
die Geschäftsvorfälle buchmäßig für längere Zeit in der Schwebe gehalten werden und 
sich hierdurch die Möglichkeit eröffnet, sie später anders darzustellen, als sie richtiger-
weise darzustellen gewesen wären, oder sie ganz außer Betracht zu lassen und im 
privaten, sich in der Buchführung nicht niederschlagenden Bereich abzuwickeln.  
Bei zeitlichen Abständen zwischen der Entstehung eines Geschäftsvorfalls und seiner 
Erfassung sind daher geeignete Maßnahmen zur Sicherung der Vollständigkeit zu 
treffen. 
48 
Kasseneinnahmen und Kassenausgaben sind nach § 146 Absatz 1 Satz 2 AO täglich 
festzuhalten. 
49 
Es ist nicht zu beanstanden, wenn Waren- und Kostenrechnungen, die innerhalb von 
acht Tagen nach Rechnungseingang oder innerhalb der ihrem gewöhnlichen Durchlauf 
durch den Betrieb entsprechenden Zeit beglichen werden, kontokorrentmäßig nicht 
(z. B. Geschäftsfreundebuch, Personenkonten) erfasst werden (vgl. R 5.2 Absatz 1 
EStR).  
50 
Werden bei der Erstellung der Bücher Geschäftsvorfälle nicht laufend, sondern nur 
periodenweise gebucht bzw. den Büchern vergleichbare Aufzeichnungen der Nicht-
buchführungspflichtigen nicht laufend, sondern nur periodenweise erstellt, dann ist 
dies unter folgenden Voraussetzungen nicht zu beanstanden: 
• Die Geschäftsvorfälle werden vorher zeitnah (bare Geschäftsvorfälle täglich, 
unbare Geschäftsvorfälle innerhalb von zehn Tagen) in Grund(buch)aufzeichnun-
gen oder Grundbüchern festgehalten und durch organisatorische Vorkehrungen ist 
sichergestellt, dass die Unterlagen bis zu ihrer Erfassung nicht verloren gehen, 
z. B. durch laufende Nummerierung der eingehenden und ausgehenden Rechnun-
gen, durch Ablage in besonderen Mappen und Ordnern oder durch elektronische 
Grund(buch)aufzeichnungen in Kassensystemen, Warenwirtschaftssystemen, 
Fakturierungssystemen etc., 
• die Vollständigkeit der Geschäftsvorfälle wird im Einzelfall gewährleistet und 


--- Page 14 ---
 
Seite 14
• es wurde zeitnah eine Zuordnung (Kontierung, mindestens aber die Zuordnung 
betrieblich / privat, Ordnungskriterium für die Ablage) vorgenommen. 
51 
Jeder Geschäftsvorfall ist periodengerecht der Abrechnungsperiode zuzuordnen, in der 
er angefallen ist. Zwingend ist die Zuordnung zum jeweiligen Geschäftsjahr oder zu 
einer nach Gesetz, Satzung oder Rechnungslegungszweck vorgeschriebenen kürzeren 
Rechnungsperiode. 
 
52 
Erfolgt die Belegsicherung oder die Erfassung von Geschäftsvorfällen unmittelbar 
nach Eingang oder Entstehung mittels DV-System (elektronische Grund(buch)auf-
zeichnungen), so stellt sich die Frage der Zumutbarkeit und Praktikabilität hinsichtlich 
der zeitgerechten Erfassung/Belegsicherung und längerer Fristen nicht. Erfüllen die 
Erfassungen Belegfunktion bzw. dienen sie der Belegsicherung (auch für Vorsysteme, 
wie Kasseneinzelaufzeichnungen und Warenwirtschaftssystem), dann ist eine unproto-
kollierte Änderung nicht mehr zulässig (siehe unter 3.2.5). Bei zeitlichen Abständen 
zwischen Erfassung und Buchung, die über den Ablauf des folgenden Monats hinaus-
gehen, sind die Ordnungsmäßigkeitsanforderungen nur dann erfüllt, wenn die 
Geschäftsvorfälle vorher fortlaufend richtig und vollständig in Grund(buch)aufzeich-
nungen oder Grundbüchern festgehalten werden (vgl. Rz. 50). Zur Erfüllung der Funk-
tion der Grund(buch)aufzeichnung vgl. Rz. 46. 
3.2.4 Ordnung (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) 
53 
Der Grundsatz der Klarheit verlangt u. a. eine systematische Erfassung und übersicht-
liche, eindeutige und nachvollziehbare Buchungen.  
54 
Die geschäftlichen Unterlagen dürfen nicht planlos gesammelt und aufbewahrt wer-
den. Ansonsten würde dies mit zunehmender Zahl und Verschiedenartigkeit der 
Geschäftsvorfälle zur Unübersichtlichkeit der Buchführung führen, einen jederzeitigen 
Abschluss unangemessen erschweren und die Gefahr erhöhen, dass Unterlagen ver-
lorengehen oder später leicht aus dem Buchführungswerk entfernt werden können. 
Hieraus folgt, dass die Bücher und Aufzeichnungen nach bestimmten Ordnungsprin-
zipien geführt werden müssen und eine Sammlung und Aufbewahrung der Belege not-
wendig ist, durch die im Rahmen des Möglichen gewährleistet wird, dass die 
Geschäftsvorfälle leicht und identifizierbar feststellbar und für einen die Lage des 
Vermögens darstellenden Abschluss unverlierbar sind (BFH-Urteil vom 26. März 
1968, BStBl II S. 527).  
55 
In der Regel verstößt die nicht getrennte Verbuchung von baren und unbaren 
Geschäftsvorfällen oder von nicht steuerbaren, steuerfreien und steuerpflichtigen 
Umsätzen ohne genügende Kennzeichnung gegen die Grundsätze der Wahrheit und 
Klarheit einer kaufmännischen Buchführung. Die nicht getrennte Aufzeichnung von 
nicht steuerbaren, steuerfreien und steuerpflichtigen Umsätzen ohne genügende 


--- Page 15 ---
 
Seite 15
Kennzeichnung verstößt in der Regel gegen steuerrechtliche Anforderungen (z. B. 
§ 22 UStG). Eine kurzzeitige gemeinsame Erfassung von baren und unbaren Tages-
geschäften im Kassenbuch ist regelmäßig nicht zu beanstanden, wenn die ursprünglich 
im Kassenbuch erfassten unbaren Tagesumsätze (z. B. EC-Kartenumsätze) gesondert 
kenntlich gemacht sind und nachvollziehbar unmittelbar nachfolgend wieder aus dem 
Kassenbuch auf ein gesondertes Konto aus- bzw. umgetragen werden, soweit die 
Kassensturzfähigkeit der Kasse weiterhin gegeben ist. 
56 
Bei der doppelten Buchführung sind die Geschäftsvorfälle so zu verarbeiten, dass sie 
geordnet darstellbar sind und innerhalb angemessener Zeit ein Überblick über die Ver-
mögens- und Ertragslage gewährleistet ist.  
57 
Die Buchungen müssen einzeln und sachlich geordnet nach Konten dargestellt (Kon-
tenfunktion) und unverzüglich lesbar gemacht werden können. Damit bei Bedarf für 
einen zurückliegenden Zeitpunkt ein Zwischenstatus oder eine Bilanz mit Gewinn- 
und Verlustrechnung aufgestellt werden kann, sind die Konten nach Abschluss-
positionen zu sammeln und nach Kontensummen oder Salden fortzuschreiben 
(Hauptbuch, siehe unter 5.4). 
3.2.5 Unveränderbarkeit (§ 146 Absatz 4 AO, § 239 Absatz 3 HGB) 
58 
Eine Buchung oder eine Aufzeichnung darf nicht in einer Weise verändert werden, 
dass der ursprüngliche Inhalt nicht mehr feststellbar ist. Auch solche Veränderungen 
dürfen nicht vorgenommen werden, deren Beschaffenheit es ungewiss lässt, ob sie 
ursprünglich oder erst später gemacht worden sind (§ 146 Absatz 4 AO, § 239 
Absatz 3 HGB). 
  
59 
Veränderungen und Löschungen von und an elektronischen Buchungen oder Auf-
zeichnungen (vgl. Rzn. 3 bis 5) müssen daher so protokolliert werden, dass die 
Voraussetzungen des § 146 Absatz 4 AO bzw. § 239 Absatz 3 HGB erfüllt sind (siehe 
auch unter 8). Für elektronische Dokumente und andere elektronische Unterlagen, die 
gem. § 147 AO aufbewahrungspflichtig und nicht Buchungen oder Aufzeichnungen 
sind, gilt dies sinngemäß.  
Beispiel 4: 
Der Steuerpflichtige erstellt über ein Fakturierungssystem Ausgangsrechnungen und 
bewahrt die inhaltlichen Informationen elektronisch auf (zum Beispiel in seinem 
Fakturierungssystem). Die Lesbarmachung der abgesandten Handels- und Geschäfts-
briefe aus dem Fakturierungssystem erfolgt jeweils unter Berücksichtigung der in den 
aktuellen Stamm- und Bewegungsdaten enthaltenen Informationen. 


--- Page 16 ---
 
Seite 16
In den Stammdaten ist im Jahr 01 der Steuersatz 16 % und der Firmenname des Kun-
den A hinterlegt. Durch Umfirmierung des Kunden A zu B und Änderung des Steuer-
satzes auf 19 % werden die Stammdaten im Jahr 02 geändert. Eine Historisierung der 
Stammdaten erfolgt nicht. 
Der Steuerpflichtige ist im Jahr 02 nicht mehr in der Lage, die inhaltliche Überein-
stimmung der abgesandten Handels- und Geschäftsbriefe mit den ursprünglichen 
Inhalten bei Lesbarmachung sicher zu stellen. 
 
60 
Der Nachweis der Durchführung der in dem jeweiligen Verfahren vorgesehenen 
Kontrollen ist u. a. durch Verarbeitungsprotokolle sowie durch die Verfahrens-
dokumentation (siehe unter 6. und unter 10.1) zu erbringen. 
4. Belegwesen (Belegfunktion) 
61 
Jeder Geschäftsvorfall ist urschriftlich bzw. als Kopie der Urschrift zu belegen.  
Ist kein Fremdbeleg vorhanden, muss ein Eigenbeleg erstellt werden. Zweck der 
Belege ist es, den sicheren und klaren Nachweis über den Zusammenhang zwischen 
den Vorgängen in der Realität einerseits und dem aufgezeichneten oder gebuchten 
Inhalt in Büchern oder sonst erforderlichen Aufzeichnungen und ihre Berechtigung 
andererseits zu erbringen (Belegfunktion). Auf die Bezeichnung als „Beleg“ kommt es 
nicht an. Die Belegfunktion ist die Grundvoraussetzung für die Beweiskraft der 
Buchführung und sonst erforderlicher Aufzeichnungen. Sie gilt auch bei Einsatz eines 
DV-Systems.  
62 
Inhalt und Umfang der in den Belegen enthaltenen Informationen sind insbesondere 
von der Belegart (z. B. Aufträge, Auftragsbestätigungen, Bescheide über Steuern oder 
Gebühren, betriebliche Kontoauszüge, Gutschriften, Lieferscheine, Lohn- und 
Gehaltsabrechnungen, Barquittungen, Rechnungen, Verträge, Zahlungsbelege) und der 
eingesetzten Verfahren abhängig.  
 
63 
Empfangene oder abgesandte Handels- oder Geschäftsbriefe erhalten erst mit dem 
Kontierungsvermerk und der Verbuchung auch die Funktion eines Buchungsbelegs. 
64 
Zur Erfüllung der Belegfunktionen sind deshalb Angaben zur Kontierung, zum 
Ordnungskriterium für die Ablage und zum Buchungsdatum auf dem Papierbeleg 
erforderlich. Bei einem elektronischen Beleg kann dies auch durch die Verbindung mit 
einem Datensatz mit Angaben zur Kontierung oder durch eine elektronische Verknüp-
fung (z. B. eindeutiger Index, Barcode) erfolgen. Ein Steuerpflichtiger hat andernfalls 
durch organisatorische Maßnahmen sicherzustellen, dass die Geschäftsvorfälle auch 
ohne Angaben auf den Belegen in angemessener Zeit progressiv und retrograd nach-
prüfbar sind.  


--- Page 17 ---
 
Seite 17
 
Korrektur- bzw. Stornobuchungen müssen auf die ursprüngliche Buchung rück-
beziehbar sein. 
65 
Ein Buchungsbeleg in Papierform oder in elektronischer Form (z. B. Rechnung) kann 
einen oder mehrere Geschäftsvorfälle enthalten.  
66 
Aus der Verfahrensdokumentation (siehe unter 10.1) muss ersichtlich sein, wie die 
elektronischen Belege erfasst, empfangen, verarbeitet, ausgegeben und aufbewahrt 
(zur Aufbewahrung siehe unter 9.) werden. 
4.1 Belegsicherung 
67 
Die Belege in Papierform oder in elektronischer Form sind zeitnah, d. h. möglichst 
unmittelbar nach Eingang oder Entstehung gegen Verlust zu sichern (vgl. zur zeit-
gerechten Belegsicherung unter 3.2.3, vgl. zur Aufbewahrung unter 9.).  
68 
Bei Papierbelegen erfolgt eine Sicherung z. B. durch laufende Nummerierung der ein-
gehenden und ausgehenden Lieferscheine und Rechnungen, durch laufende Ablage in 
besonderen Mappen und Ordnern, durch zeitgerechte Erfassung in Grund(buch)auf-
zeichnungen oder durch laufende Vergabe eines Barcodes und anschließende bildliche 
Erfassung der Papierbelege im Sinne des § 147 Absatz 2 AO (siehe Rz. 130). 
69 
Bei elektronischen Belegen (z. B. Abrechnung aus Fakturierung) kann die laufende 
Nummerierung automatisch vergeben werden (z. B. durch eine eindeutige Beleg-
nummer).  
70 
Die Belegsicherung kann organisatorisch und technisch mit der Zuordnung zwischen 
Beleg und Grund(buch)aufzeichnung oder Buchung verbunden werden. 
4.2 Zuordnung zwischen Beleg und Grund(buch)aufzeichnung oder Buchung 
71 
Die Zuordnung zwischen dem einzelnen Beleg und der dazugehörigen Grund(buch)auf-
zeichnung oder Buchung kann anhand von eindeutigen Zuordnungsmerkmalen (z. B. 
Index, Paginiernummer, Dokumenten-ID) und zusätzlichen Identifikationsmerkmalen 
für die Papierablage oder für die Such- und Filtermöglichkeit bei elektronischer Beleg-
ablage gewährleistet werden. Gehören zu einer Grund(buch)aufzeichnung oder Buchung 
mehrere Belege (z. B. Rechnung verweist für Menge und Art der gelieferten Gegenstän-
de nur auf Lieferschein), bedarf es zusätzlicher Zuordnungs- und Identifikationsmerk-
male für die Verknüpfung zwischen den Belegen und der Grund(buch)aufzeichnung 
oder Buchung. 
72 
Diese Zuordnungs- und Identifizierungsmerkmale aus dem Beleg müssen bei der Auf-
zeichnung oder Verbuchung in die Bücher oder Aufzeichnungen übernommen werden, 
um eine progressive und retrograde Prüfbarkeit zu ermöglichen. 


--- Page 18 ---
 
Seite 18
73 
Die Ablage der Belege und die Zuordnung zwischen Beleg und Aufzeichnung müssen 
in angemessener Zeit nachprüfbar sein. So kann z. B. Beleg- oder Buchungsdatum, 
Kontoauszugnummer oder Name bei umfangreichem Beleganfall mangels Eindeutig-
keit in der Regel kein geeignetes Zuordnungsmerkmal für den einzelnen Geschäftsvor-
fall sein. 
74 
Beispiel 5: 
Ein Steuerpflichtiger mit ausschließlich unbaren Geschäftsvorfällen erhält nach 
Abschluss eines jeden Monats von seinem Kreditinstitut einen Kontoauszug in 
Papierform mit vielen einzelnen Kontoblättern. Für die Zuordnung der Belege und 
Aufzeichnungen erfasst der Unternehmer ausschließlich die Kontoauszugsnummer. 
Allein anhand der Kontoauszugsnummer - ohne zusätzliche Angabe der Blattnummer 
und der Positionsnummer - ist eine Zuordnung von Beleg und Aufzeichnung oder 
Buchung in angemessener Zeit nicht nachprüfbar. 
4.3 Erfassungsgerechte Aufbereitung der Buchungsbelege 
75 
Eine erfassungsgerechte Aufbereitung der Buchungsbelege in Papierform oder die 
entsprechende Übernahme von Beleginformationen aus elektronischen Belegen 
(Daten, Datensätze, elektronische Dokumente und elektronische Unterlagen) ist 
sicherzustellen. Diese Aufbereitung der Belege ist insbesondere bei Fremdbelegen von 
Bedeutung, da der Steuerpflichtige im Allgemeinen keinen Einfluss auf die Gestaltung 
der ihm zugesandten Handels- und Geschäftsbriefe (z. B. Eingangsrechnungen) hat. 
 
76 
Werden neben bildhaften Urschriften auch elektronische Meldungen bzw. Datensätze 
ausgestellt (identische Mehrstücke derselben Belegart), ist die Aufbewahrung der 
tatsächlich weiterverarbeiteten Formate (buchungsbegründende Belege) ausreichend, 
sofern diese über die höchste maschinelle Auswertbarkeit verfügen. In diesem Fall 
erfüllt das Format mit der höchsten maschinellen Auswertbarkeit mit dessen 
vollständigem Dateninhalt die Belegfunktion und muss mit dessen vollständigem 
Inhalt gespeichert werden. Andernfalls sind beide Formate aufzubewahren. Dies gilt 
entsprechend, wenn mehrere elektronische Meldungen bzw. mehrere Datensätze ohne 
bildhafte Urschrift ausgestellt werden. Dies gilt auch für elektronische Meldungen 
(strukturierte Daten, wie z. B. ein monatlicher Kontoauszug im CSV-Format oder als 
XML-File), für die inhaltsgleiche bildhafte Dokumente zusätzlich bereitgestellt 
werden. Eine zusätzliche Archivierung der inhaltsgleichen Kontoauszüge in PDF oder 
Papier kann bei Erfüllung der Belegfunktion durch die strukturierten 
Kontoumsatzdaten entfallen.  
Bei Einsatz eines Fakturierungsprogramms muss unter Berücksichtigung der vorge-
nannten Voraussetzungen keine bildhafte Kopie der Ausgangsrechnung (z. B. in Form 


--- Page 19 ---
 
Seite 19
einer PDF-Datei) ab Erstellung gespeichert bzw. aufbewahrt werden, wenn jederzeit 
auf Anforderung ein entsprechendes Doppel der Ausgangsrechnung erstellt werden 
kann. 
Hierfür sind u. a. folgende Voraussetzungen zu beachten: 
• Entsprechende Stammdaten (z. B. Debitoren, Warenwirtschaft etc.) werden 
laufend historisiert 
• AGB werden ebenfalls historisiert und aus der Verfahrensdokumentation ist 
ersichtlich, welche AGB bei Erstellung der Originalrechnung verwendet 
wurden 
• Originallayout des verwendeten Geschäftsbogens wird als Muster (Layer) 
gespeichert und bei Änderungen historisiert. Zudem ist aus der Verfahrens-
dokumentation ersichtlich, welches Format bei Erstellung der Originalrech-
nung verwendet wurde (idealerweise kann bei Ausdruck oder Lesbarmachung 
des Rechnungsdoppels dieses Originallayout verwendet werden). 
• Weiterhin sind die Daten des Fakturierungsprogramms in maschinell auswert-
barer Form und unveränderbar aufzubewahren. 
77 
Jedem Geschäftsvorfall muss ein Beleg zugrunde liegen, mit folgenden Inhalten: 
 
Bezeichnung 
Begründung 
Eindeutige Belegnummer (z. B. Index, 
Paginiernummer, Dokumenten-ID, 
fortlaufende 
Rechnungsausgangsnummer) 
  
Angabe zwingend (§ 146 Absatz 1 Satz 1 
AO, einzeln, vollständig, geordnet) 
Kriterium für Vollständigkeitskontrolle 
(Belegsicherung) 
Bei umfangreichem Beleganfall ist Zuord-
nung und Identifizierung regelmäßig nicht 
aus Belegdatum oder anderen Merkmalen 
eindeutig ableitbar. 
Sofern die Fremdbelegnummer eine ein-
deutige Zuordnung zulässt, kann auch 
diese verwendet werden. 
Belegaussteller und -empfänger 
Soweit dies zu den branchenüblichen Min-
destaufzeichnungspflichten gehört und 
keine Aufzeichnungserleichterungen 
bestehen (z. B. § 33 UStDV) 
Betrag bzw. Mengen- oder Wertanga-
ben, aus denen sich der zu buchende 
Angabe zwingend (BFH vom 12. Mai 
1966, BStBl III S. 371); Dokumentation 


--- Page 20 ---
 
Seite 20
Bezeichnung 
Begründung 
Betrag ergibt 
einer Veränderung des Anlage- und 
Umlaufvermögens sowie des Eigen- und 
Fremdkapitals 
Währungsangabe und Wechselkurs bei 
Fremdwährung 
Ermittlung des Buchungsbetrags 
Hinreichende Erläuterung des 
Geschäftsvorfalls 
Hinweis auf BFH-Urteil vom 12. Mai 
1966, BStBl III S. 371; BFH-Urteil vom 
1. Oktober 1969, BStBl II 1970 S. 45 
 
Belegdatum 
 
Angabe zwingend (§ 146 Absatz 1 Satz 1 
AO, zeitgerecht). 
Identifikationsmerkmale für eine 
chronologische Erfassung, bei 
Bargeschäften regelmäßig Zeitpunkt des 
Geschäftsvorfalls 
Evtl. zusätzliche Erfassung der Belegzeit 
bei umfangreichem Beleganfall 
erforderlich 
Verantwortlicher Aussteller, soweit 
vorhanden 
Z. B. Bediener der Kasse 
 
Vgl. Rz. 85 zu den Inhalten der Grund(buch)aufzeichnungen. 
Vgl. Rz. 94 zu den Inhalten des Journals.  
78 
Für umsatzsteuerrechtliche Zwecke können weitere Angaben erforderlich sein.  
Dazu gehören beispielsweise die Rechnungsangaben nach §§ 14, 14a UStG und § 33 
UStDV. 
79 
Buchungsbelege sowie abgesandte oder empfangene Handels- oder Geschäftsbriefe in 
Papierform oder in elektronischer Form enthalten darüber hinaus vielfach noch weitere 
Informationen, die zum Verständnis und zur Überprüfung der für die Besteuerung 
gesetzlich vorgeschriebenen Aufzeichnungen im Einzelfall von Bedeutung und damit 
ebenfalls aufzubewahren sind. Dazu gehören z. B.: 
• Mengen- oder Wertangaben zur Erläuterung des Buchungsbetrags, sofern nicht 
bereits unter Rz. 77 berücksichtigt, 


--- Page 21 ---
 
Seite 21
• Einzelpreis (z. B. zur Bewertung), 
• Valuta, Fälligkeit (z. B. zur Bewertung), 
• Angaben zu Skonti, Rabatten (z. B. zur Bewertung), 
• Zahlungsart (bar, unbar), 
• Angaben zu einer Steuerbefreiung.  
4.4 Besonderheiten 
80 
Bei DV-gestützten Prozessen wird der Nachweis der zutreffenden Abbildung von 
Geschäftsvorfällen oft nicht durch konventionelle Belege erbracht (z. B. Buchungen 
aus Fakturierungssätzen, die durch Multiplikation von Preisen mit entnommenen Men-
gen aus der Betriebsdatenerfassung gebildet werden). Die Erfüllung der Belegfunktion 
ist dabei durch die ordnungsgemäße Anwendung des jeweiligen Verfahrens wie folgt 
nachzuweisen: 
• Dokumentation der programminternen Vorschriften zur Generierung der 
Buchungen, 
• Nachweis oder Bestätigung, dass die in der Dokumentation enthaltenen Vorschrif-
ten einem autorisierten Änderungsverfahren unterlegen haben (u. a. Zugriffs-
schutz, Versionsführung, Test- und Freigabeverfahren), 
• Nachweis der Anwendung des genehmigten Verfahrens sowie 
• Nachweis der tatsächlichen Durchführung der einzelnen Buchungen. 
 
81 
Bei Dauersachverhalten sind die Ursprungsbelege Basis für die folgenden Automatik-
buchungen. Bei (monatlichen) AfA-Buchungen nach Anschaffung eines abnutzbaren 
Wirtschaftsguts ist der Anschaffungsbeleg mit der AfA-Bemessungsgrundlage und 
weiteren Parametern (z. B. Nutzungsdauer) aufbewahrungspflichtig. Aus der Verfah-
rensdokumentation und der ordnungsmäßigen Anwendung des Verfahrens muss der 
automatische Buchungsvorgang nachvollziehbar sein. 
5. Aufzeichnung der Geschäftsvorfälle in zeitlicher Reihenfolge und in sach-
licher Ordnung (Grund(buch)aufzeichnungen, Journal- und 
Kontenfunktion) 
82 
Der Steuerpflichtige hat organisatorisch und technisch sicherzustellen, dass die elek-
tronischen Buchungen und sonst erforderlichen elektronischen Aufzeichnungen 
einzeln, vollständig, richtig, zeitgerecht und geordnet vorgenommen werden (§ 146 
Absatz 1 Satz 1 AO, § 239 Absatz 2 HGB). Jede Buchung oder Aufzeichnung muss im 
Zusammenhang mit einem Beleg stehen (BFH-Urteil vom 24. Juni 1997, BStBl II 
1998 S. 51).  


--- Page 22 ---
 
Seite 22
83 
Bei der doppelten Buchführung müssen alle Geschäftsvorfälle in zeitlicher Reihen-
folge (Grund(buch)aufzeichnung, Journalfunktion) und in sachlicher Gliederung 
(Hauptbuch, Kontenfunktion, siehe unter 5.4) darstellbar sein. Im Hauptbuch bzw. bei 
der Kontenfunktion verursacht jeder Geschäftsvorfall eine Buchung auf mindestens 
zwei Konten (Soll- und Habenbuchung).  
84 
Die Erfassung der Geschäftsvorfälle in elektronischen Grund(buch)aufzeichnungen 
(siehe unter 5.1 und 5.2) und die Verbuchung im Journal (siehe unter 5.3) kann organi-
satorisch und zeitlich auseinanderfallen (z. B. Grund(buch)aufzeichnung in Form von 
Kassenauftragszeilen). Erfüllen die Erfassungen Belegfunktion bzw. dienen sie der 
Belegsicherung, dann ist eine unprotokollierte Änderung nicht mehr zulässig (vgl. 
Rzn. 58 und 59). In diesen Fällen gelten die Ordnungsvorschriften bereits mit der 
ersten Erfassung der Geschäftsvorfälle und der Daten und müssen über alle nachfol-
genden Prozesse erhalten bleiben (z. B. Übergabe von Daten aus Vor- in Haupt-
systeme). 
5.1 Erfassung in Grund(buch)aufzeichnungen 
85 
Die fortlaufende Aufzeichnung der Geschäftsvorfälle erfolgt zunächst in Papierform 
oder in elektronischen Grund(buch)aufzeichnungen (Grundaufzeichnungsfunktion), 
um die Belegsicherung und die Garantie der Unverlierbarkeit des Geschäftsvorfalls zu 
gewährleisten. Sämtliche Geschäftsvorfälle müssen der zeitlichen Reihenfolge nach 
und materiell mit ihrem richtigen und erkennbaren Inhalt festgehalten werden. 
 
Zu den aufzeichnungspflichtigen Inhalten gehören 
• die in Rzn. 77, 78 und 79 enthaltenen Informationen, 
• das Erfassungsdatum, soweit abweichend vom Buchungsdatum 
Begründung: 
o Angabe zwingend (§ 146 Absatz 1 Satz 1 AO, zeitgerecht), 
o Zeitpunkt der Buchungserfassung und -verarbeitung, 
o Angabe der „Festschreibung“ (Veränderbarkeit nur mit Protokollie-
rung) zwingend, soweit nicht Unveränderbarkeit automatisch mit Erfas-
sung und Verarbeitung in Grund(buch)aufzeichnung. 
 
Vgl. Rz. 94 zu den Inhalten des Journals.  
86 
Die Grund(buch)aufzeichnungen sind nicht an ein bestimmtes System gebunden.  
Jedes System, durch das die einzelnen Geschäftsvorfälle fortlaufend, vollständig und 
richtig festgehalten werden, so dass die Grundaufzeichnungsfunktion erfüllt wird, ist 
ordnungsmäßig (vgl. BFH-Urteil vom 26. März 1968, BStBl II S. 527 für Buchfüh-
rungspflichtige).  


--- Page 23 ---
 
Seite 23
5.2 Digitale Grund(buch)aufzeichnungen 
87 
Sowohl beim Einsatz von Haupt- als auch von Vor- oder Nebensystemen ist eine 
Verbuchung im Journal des Hauptsystems (z. B. Finanzbuchhaltung) bis zum Ablauf 
des folgenden Monats nicht zu beanstanden, wenn die einzelnen Geschäftsvorfälle 
bereits in einem Vor- oder Nebensystem die Grundaufzeichnungsfunktion erfüllen und 
die Einzeldaten aufbewahrt werden.  
88 
Durch Erfassungs-, Übertragungs- und Verarbeitungskontrollen ist sicherzustellen, 
dass alle Geschäftsvorfälle vollständig erfasst oder übermittelt werden und danach 
nicht unbefugt (d. h. nicht ohne Zugriffsschutzverfahren) und nicht ohne Nachweis des 
vorausgegangenen Zustandes verändert werden können. Die Durchführung der Kon-
trollen ist zu protokollieren. Die konkrete Ausgestaltung der Protokollierung ist 
abhängig von der Komplexität und Diversifikation der Geschäftstätigkeit und der 
Organisationsstruktur sowie des eingesetzten DV-Systems.  
89 
Neben den Daten zum Geschäftsvorfall selbst müssen auch alle für die Verarbeitung 
erforderlichen Tabellendaten (Stammdaten, Bewegungsdaten, Metadaten wie z. B. 
Grund- oder Systemeinstellungen, geänderte Parameter), deren Historisierung und 
Programme gespeichert sein. Dazu gehören auch Informationen zu Kriterien, die eine 
Abgrenzung zwischen den steuerrechtlichen, den handelsrechtlichen und anderen 
Buchungen (z. B. nachrichtliche Datensätze zu Fremdwährungen, alternative Bewer-
tungsmethoden, statistische Buchungen, GuV-Kontennullstellungen, Summenkonten) 
ermöglichen. 
5.3 Verbuchung im Journal (Journalfunktion) 
90 
Die Journalfunktion erfordert eine vollständige, zeitgerechte und formal richtige Erfas-
sung, Verarbeitung und Wiedergabe der eingegebenen Geschäftsvorfälle. Sie dient 
dem Nachweis der tatsächlichen und zeitgerechten Verarbeitung der Geschäftsvorfälle. 
91 
Werden die unter 5.1 genannten Voraussetzungen bereits mit fortlaufender Verbu-
chung im Journal erfüllt, ist eine zusätzliche Erfassung in Grund(buch)aufzeichnungen 
nicht erforderlich. Eine laufende Aufzeichnung unmittelbar im Journal genügt den 
Erfordernissen der zeitgerechten Erfassung in Grund(buch)aufzeichnungen (vgl. BFH-
Urteil vom 16. September 1964, BStBl III S. 654). Zeitversetzte Buchungen im 
Journal genügen nur dann, wenn die Geschäftsvorfälle vorher fortlaufend richtig und 
vollständig in Grundaufzeichnungen oder Grundbüchern aufgezeichnet werden.  
Die Funktion der Grund(buch)aufzeichnungen kann auf Dauer auch durch eine geord-
nete und übersichtliche Belegablage erfüllt werden (§ 239 Absatz 4 HGB, § 146 
Absatz 5 AO, H 5.2 „Grundbuchaufzeichnungen“ EStH; vgl. Rz. 46).  


--- Page 24 ---
 
Seite 24
92 
Die Journalfunktion ist nur erfüllt, wenn die gespeicherten Aufzeichnungen gegen 
Veränderung oder Löschung geschützt sind.  
93 
Fehlerhafte Buchungen können wirksam und nachvollziehbar durch Stornierungen 
oder Neubuchungen geändert werden (siehe unter 8.). Es besteht deshalb weder ein 
Bedarf noch die Notwendigkeit für weitere nachträgliche Veränderungen einer einmal 
erfolgten Buchung. Bei der doppelten Buchführung kann die Journalfunktion 
zusammen mit der Kontenfunktion erfüllt werden, indem bereits bei der erstmaligen 
Erfassung des Geschäftsvorfalls alle für die sachliche Zuordnung notwendigen 
Informationen erfasst werden. 
 
94 
Zur Erfüllung der Journalfunktion und zur Ermöglichung der Kontenfunktion sind bei 
der Buchung insbesondere die nachfolgenden Angaben zu erfassen oder bereit zu 
stellen: 
• Eindeutige Belegnummer (siehe Rz. 77), 
• Buchungsbetrag (siehe Rz. 77), 
• Währungsangabe und Wechselkurs bei Fremdwährung (siehe Rz. 77), 
• Hinreichende Erläuterung des Geschäftsvorfalls (siehe Rz. 77) - kann (bei 
Erfüllung der Journal- und Kontenfunktion) im Einzelfall bereits durch andere in 
Rz. 94 aufgeführte Angaben gegeben sein, 
• Belegdatum, soweit nicht aus den Grundaufzeichnungen ersichtlich (siehe Rzn. 77 
und 85) 
• Buchungsdatum, 
• Erfassungsdatum, soweit nicht aus der Grundaufzeichnung ersichtlich (siehe 
Rz. 85), 
• Autorisierung soweit vorhanden, 
• Buchungsperiode/Voranmeldungszeitraum (Ertragsteuer/Umsatzsteuer), 
• Umsatzsteuersatz (siehe Rz. 78), 
• Steuerschlüssel, soweit vorhanden (siehe Rz. 78), 
• Umsatzsteuerbetrag (siehe Rz. 78), 
• Umsatzsteuerkonto (siehe Rz. 78), 
• Umsatzsteuer-Identifikationsnummer (siehe Rz. 78), 
• Steuernummer (siehe Rz. 78), 
• Konto und Gegenkonto, 
• Buchungsschlüssel (soweit vorhanden), 
• Soll- und Haben-Betrag, 
• eindeutige Identifikationsnummer (Schlüsselfeld) des Geschäftsvorfalls (soweit 
Aufteilung der Geschäftsvorfälle in Teilbuchungssätze [Buchungs-Halbsätze] oder 
zahlreiche Soll- oder Habenkonten [Splitbuchungen] vorhanden). Über die einheit-


--- Page 25 ---
 
Seite 25
liche und je Wirtschaftsjahr eindeutige Identifikationsnummer des Geschäftsvor-
falls muss die Identifizierung und Zuordnung aller Teilbuchungen einschließlich 
Steuer-, Sammel-, Verrechnungs- und Interimskontenbuchungen eines Geschäfts-
vorfalls gewährleistet sein.  
5.4 Aufzeichnung der Geschäftsvorfälle in sachlicher Ordnung (Hauptbuch) 
95 
Die Geschäftsvorfälle sind so zu verarbeiten, dass sie geordnet darstellbar sind 
(Kontenfunktion) und damit die Grundlage für einen Überblick über die Vermögens- 
und Ertragslage darstellen. Zur Erfüllung der Kontenfunktion bei Bilanzierenden 
müssen Geschäftsvorfälle nach Sach- und Personenkonten geordnet dargestellt 
werden.  
96 
Die Kontenfunktion verlangt, dass die im Journal in zeitlicher Reihenfolge einzeln 
aufgezeichneten Geschäftsvorfälle auch in sachlicher Ordnung auf Konten dargestellt 
werden. Damit bei Bedarf für einen zurückliegenden Zeitpunkt ein Zwischenstatus 
oder eine Bilanz mit Gewinn- und Verlustrechnung aufgestellt werden kann, müssen 
Eröffnungsbilanzbuchungen und alle Abschlussbuchungen in den Konten enthalten 
sein. Die Konten sind nach Abschlussposition zu sammeln und nach Kontensummen 
oder Salden fortzuschreiben.  
97 
Werden innerhalb verschiedener Bereiche des DV-Systems oder zwischen unter-
schiedlichen DV-Systemen differierende Ordnungskriterien verwendet, so müssen 
entsprechende Zuordnungstabellen (z. B. elektronische Mappingtabellen) vorgehalten 
werden (z. B. Wechsel des Kontenrahmens, unterschiedliche Nummernkreise in Vor- 
und Hauptsystem). Dies gilt auch bei einer elektronischen Übermittlung von Daten an 
die Finanzbehörde (z. B. unterschiedliche Ordnungskriterien in Bilanz/GuV und EÜR 
einerseits und USt-Voranmeldung, LSt-Anmeldung, Anlage EÜR und E-Bilanz ande-
rerseits). Sollte die Zuordnung mit elektronischen Verlinkungen oder Schlüsselfeldern 
erfolgen, sind die Verlinkungen in dieser Form vorzuhalten.  
98 
Die vorstehenden Ausführungen gelten für die Nebenbücher entsprechend. 
99 
Bei der Übernahme verdichteter Zahlen ins Hauptsystem müssen die zugehörigen Ein-
zelaufzeichnungen aus den Vor- und Nebensystemen erhalten bleiben. 
6. Internes Kontrollsystem (IKS) 
100 
Für die Einhaltung der Ordnungsvorschriften des § 146 AO (siehe unter 3.) hat der 
Steuerpflichtige Kontrollen einzurichten, auszuüben und zu protokollieren.  
Hierzu gehören beispielsweise 
• Zugangs- und Zugriffsberechtigungskontrollen auf Basis entsprechender Zugangs- 
und Zugriffsberechtigungskonzepte (z. B. spezifische Zugangs- und 


--- Page 26 ---
 
Seite 26
Zugriffsberechtigungen), 
• Funktionstrennungen, 
• Erfassungskontrollen (Fehlerhinweise, Plausibilitätsprüfungen), 
• Abstimmungskontrollen bei der Dateneingabe, 
• Verarbeitungskontrollen, 
• Schutzmaßnahmen gegen die beabsichtigte und unbeabsichtigte Verfälschung von 
Programmen, Daten und Dokumenten. 
Die konkrete Ausgestaltung des Kontrollsystems ist abhängig von der Komplexität 
und Diversifikation der Geschäftstätigkeit und der Organisationsstruktur sowie des 
eingesetzten DV-Systems. 
101 
Im Rahmen eines funktionsfähigen IKS muss auch anlassbezogen (z. B. System-
wechsel) geprüft werden, ob das eingesetzte DV-System tatsächlich dem dokumen-
tierten System entspricht (siehe Rz. 155 zu den Rechtsfolgen bei fehlender oder unge-
nügender Verfahrensdokumentation). 
102 
Die Beschreibung des IKS ist Bestandteil der Verfahrensdokumentation (siehe 
unter 10.1). 
7. Datensicherheit 
103 
Der Steuerpflichtige hat sein DV-System gegen Verlust (z. B. Unauffindbarkeit, Ver-
nichtung, Untergang und Diebstahl) zu sichern und gegen unberechtigte Eingaben und 
Veränderungen (z. B. durch Zugangs- und Zugriffskontrollen) zu schützen. 
104 
Werden die Daten, Datensätze, elektronischen Dokumente und elektronischen Unter-
lagen nicht ausreichend geschützt und können deswegen nicht mehr vorgelegt werden, 
so ist die Buchführung formell nicht mehr ordnungsmäßig. 
105 
Beispiel 6: 
Unternehmer überschreibt unwiderruflich die Finanzbuchhaltungsdaten des Vorjahres 
mit den Daten des laufenden Jahres. 
Die sich daraus ergebenden Rechtsfolgen sind vom jeweiligen Einzelfall abhängig. 
106 
Die Beschreibung der Vorgehensweise zur Datensicherung ist Bestandteil der Verfah-
rensdokumentation (siehe unter 10.1). Die konkrete Ausgestaltung der Beschreibung 
ist abhängig von der Komplexität und Diversifikation der Geschäftstätigkeit und der 
Organisationsstruktur sowie des eingesetzten DV-Systems. 


--- Page 27 ---
 
Seite 27
8. Unveränderbarkeit, Protokollierung von Änderungen 
107 
Nach § 146 Absatz 4 AO darf eine Buchung oder Aufzeichnung nicht in einer Weise 
verändert werden, dass der ursprüngliche Inhalt nicht mehr feststellbar ist. Auch sol-
che Veränderungen dürfen nicht vorgenommen werden, deren Beschaffenheit es unge-
wiss lässt, ob sie ursprünglich oder erst später gemacht worden sind. 
108 
Das zum Einsatz kommende DV-Verfahren muss die Gewähr dafür bieten, dass alle 
Informationen (Programme und Datenbestände), die einmal in den Verarbeitungs-
prozess eingeführt werden (Beleg, Grundaufzeichnung, Buchung), nicht mehr unter-
drückt oder ohne Kenntlichmachung überschrieben, gelöscht, geändert oder verfälscht 
werden können. Bereits in den Verarbeitungsprozess eingeführte Informationen 
(Beleg, Grundaufzeichnung, Buchung) dürfen nicht ohne Kenntlichmachung durch 
neue Daten ersetzt werden. 
109 
Beispiele 7 für unzulässige Vorgänge: 
• Elektronische Grund(buch)aufzeichnungen aus einem Kassen- oder Warenwirt-
schaftssystem werden über eine Datenschnittstelle in ein Officeprogramm expor-
tiert, dort unprotokolliert editiert und anschließend über eine Datenschnittstelle 
reimportiert. 
• Vorerfassungen und Stapelbuchungen werden bis zur Erstellung des Jahresab-
schlusses und darüber hinaus offen gehalten. Alle Eingaben können daher 
unprotokolliert geändert werden. 
 
110 
Die Unveränderbarkeit der Daten, Datensätze, elektronischen Dokumente und elektro-
nischen Unterlagen (vgl. Rzn. 3 bis 5) kann sowohl hardwaremäßig (z. B. unveränder-
bare und fälschungssichere Datenträger) als auch softwaremäßig (z. B. Sicherungen, 
Sperren, Festschreibung, Löschmerker, automatische Protokollierung, Historisierun-
gen, Versionierungen) als auch organisatorisch (z. B. mittels Zugriffsberechtigungs-
konzepten) gewährleistet werden. Die Ablage von Daten und elektronischen Doku-
menten in einem Dateisystem erfüllt die Anforderungen der Unveränderbarkeit 
regelmäßig nicht, soweit nicht zusätzliche Maßnahmen ergriffen werden, die eine 
Unveränderbarkeit gewährleisten.  
111 
Spätere Änderungen sind ausschließlich so vorzunehmen, dass sowohl der ursprüng-
liche Inhalt als auch die Tatsache, dass Veränderungen vorgenommen wurden, 
erkennbar bleiben. Bei programmgenerierten bzw. programmgesteuerten Aufzeich-
nungen (automatisierte Belege bzw. Dauerbelege) sind Änderungen an den der Auf-
zeichnung zugrunde liegenden Generierungs- und Steuerungsdaten ebenfalls aufzu-
zeichnen. Dies betrifft insbesondere die Protokollierung von Änderungen in Einstel-
lungen oder die Parametrisierung der Software. Bei einer Änderung von Stammdaten 


--- Page 28 ---
 
Seite 28
(z. B. Abkürzungs- oder Schlüsselverzeichnisse, Organisationspläne) muss die ein-
deutige Bedeutung in den entsprechenden Bewegungsdaten (z. B. Umsatzsteuer-
schlüssel, Währungseinheit, Kontoeigenschaft) erhalten bleiben. Ggf. müssen Stamm-
datenänderungen ausgeschlossen oder Stammdaten mit Gültigkeitsangaben historisiert 
werden, um mehrdeutige Verknüpfungen zu verhindern. Auch eine Änderungshistorie 
darf nicht nachträglich veränderbar sein. 
112 
Werden Systemfunktionalitäten oder Manipulationsprogramme eingesetzt, die diesen 
Anforderungen entgegenwirken, führt dies zur Ordnungswidrigkeit der elektronischen 
Bücher und sonst erforderlicher elektronischer Aufzeichnungen. 
Beispiel 8: 
 
Einsatz von Zappern, Phantomware, Backofficeprodukten mit dem Ziel unproto-
kollierter Änderungen elektronischer Einnahmenaufzeichnungen. 
9. Aufbewahrung  
113 
Der sachliche Umfang der Aufbewahrungspflicht in § 147 Absatz 1 AO besteht 
grundsätzlich nur im Umfang der Aufzeichnungspflicht (BFH-Urteil vom 24. Juni 
2009, BStBl II 2010 S. 452; BFH-Urteil vom 26. Februar 2004, BStBl II S. 599). 
114 
Müssen Bücher für steuerliche Zwecke geführt werden, sind sie in vollem Umfang 
aufbewahrungs- und vorlagepflichtig (z. B. Finanzbuchhaltung hinsichtlich Drohver-
lustrückstellungen, nicht abziehbare Betriebsausgaben, organschaftliche Steuer-
umlagen; BFH-Beschluss vom 26. September 2007, BStBl II 2008 S. 415). 
115 
Auch Steuerpflichtige, die nach § 4 Absatz 3 EStG als Gewinn den Überschuss der 
Betriebseinnahmen über die Betriebsausgaben ansetzen, sind verpflichtet, Aufzeich-
nungen und Unterlagen nach § 147 Absatz 1 AO aufzubewahren (BFH-Urteil vom 
24. Juni 2009, BStBl II 2010 S. 452; BFH-Urteil vom 26. Februar 2004, BStBl II 
S. 599). 
116 
Aufbewahrungspflichten können sich auch aus anderen Rechtsnormen (z. B. § 14b 
UStG) ergeben. 
117 
Die aufbewahrungspflichtigen Unterlagen müssen geordnet aufbewahrt werden. Ein 
bestimmtes Ordnungssystem ist nicht vorgeschrieben. Die Ablage kann z. B. nach 
Zeitfolge, Sachgruppen, Kontenklassen, Belegnummern oder alphabetisch erfolgen. 
Bei elektronischen Unterlagen ist ihr Eingang, ihre Archivierung und ggf. Konver-
tierung sowie die weitere Verarbeitung zu protokollieren. Es muss jedoch sicherge-
stellt sein, dass ein sachverständiger Dritter innerhalb angemessener Zeit prüfen kann. 
118 
Die nach außersteuerlichen und steuerlichen Vorschriften aufzeichnungspflichtigen 
und nach § 147 Absatz 1 AO aufbewahrungspflichtigen Unterlagen können nach § 147 
Absatz 2 AO bis auf wenige Ausnahmen auch als Wiedergabe auf einem Bildträger 


--- Page 29 ---
 
Seite 29
oder auf anderen Datenträgern aufbewahrt werden, wenn dies den GoB entspricht und 
sichergestellt ist, dass die Wiedergabe oder die Daten 
1. mit den empfangenen Handels- oder Geschäftsbriefen und den Buchungsbelegen 
bildlich und mit den anderen Unterlagen inhaltlich übereinstimmen, wenn sie 
lesbar gemacht werden, 
1. während der Dauer der Aufbewahrungsfrist jederzeit verfügbar sind, unverzüglich 
lesbar gemacht und maschinell ausgewertet werden können. 
119 
Sind aufzeichnungs- und aufbewahrungspflichtige Daten, Datensätze, elektronische 
Dokumente und elektronische Unterlagen im Unternehmen entstanden oder dort ein-
gegangen, sind sie auch in dieser Form aufzubewahren und dürfen vor Ablauf der Auf-
bewahrungsfrist nicht gelöscht werden. Sie dürfen daher nicht mehr ausschließlich in 
ausgedruckter Form aufbewahrt werden und müssen für die Dauer der Aufbewah-
rungsfrist unveränderbar erhalten bleiben (z. B. per E-Mail eingegangene Rechnung im 
PDF-Format oder bildlich erfasste Papierbelege). Dies gilt unabhängig davon, ob die 
Aufbewahrung im Produktivsystem oder durch Auslagerung in ein anderes DV-System 
erfolgt. Unter Zumutbarkeitsgesichtspunkten ist es nicht zu beanstanden, wenn der 
Steuerpflichtige elektronisch erstellte und in Papierform abgesandte Handels- und 
Geschäftsbriefe nur in Papierform aufbewahrt. 
120 
Beispiel 9 zu Rz. 119: 
Ein Steuerpflichtiger erstellt seine Ausgangsrechnungen mit einem Textverarbeitungs-
programm. Nach dem Ausdruck der jeweiligen Rechnung wird die hierfür verwendete 
Maske (Dokumentenvorlage) mit den Inhalten der nächsten Rechnung überschrieben. 
Es ist in diesem Fall nicht zu beanstanden, wenn das Doppel des versendeten Schrei-
bens in diesem Fall nur als Papierdokument aufbewahrt wird. Werden die abgesandten 
Handels- und Geschäftsbriefe jedoch tatsächlich in elektronischer Form aufbewahrt 
(z. B. im File-System oder einem DMS-System), so ist eine ausschließliche Aufbe-
wahrung in Papierform nicht mehr zulässig. Das Verfahren muss dokumentiert wer-
den. Werden Handels- oder Geschäftsbriefe mit Hilfe eines Fakturierungssystems oder 
ähnlicher Anwendungen erzeugt, bleiben die elektronischen Daten aufbewahrungs-
pflichtig. 
121 
Bei den Daten und Dokumenten ist - wie bei den Informationen in Papierbelegen - auf 
deren Inhalt und auf deren Funktion abzustellen, nicht auf deren Bezeichnung. So sind 
beispielsweise E-Mails mit der Funktion eines Handels- oder Geschäftsbriefs oder 
eines Buchungsbelegs in elektronischer Form aufbewahrungspflichtig. Dient eine 
E-Mail nur als „Transportmittel“, z. B. für eine angehängte elektronische Rechnung, 
und enthält darüber hinaus keine weitergehenden aufbewahrungspflichtigen Informa-
tionen, so ist diese nicht aufbewahrungspflichtig (wie der bisherige Papierbriefum-
schlag). 


--- Page 30 ---
 
Seite 30
122 
Ein elektronisches Dokument ist mit einem nachvollziehbaren und eindeutigen Index 
zu versehen. Der Erhalt der Verknüpfung zwischen Index und elektronischem Doku-
ment muss während der gesamten Aufbewahrungsfrist gewährleistet sein. Es ist 
sicherzustellen, dass das elektronische Dokument unter dem zugeteilten Index ver-
waltet werden kann. Stellt ein Steuerpflichtiger durch organisatorische Maßnahmen 
sicher, dass das elektronische Dokument auch ohne Index verwaltet werden kann, und 
ist dies in angemessener Zeit nachprüfbar, so ist aus diesem Grund die Buchführung 
nicht zu beanstanden.  
123 
Das Anbringen von Buchungsvermerken, Indexierungen, Barcodes, farblichen Hervor-
hebungen usw. darf - unabhängig von seiner technischen Ausgestaltung - keinen Ein-
fluss auf die Lesbarmachung des Originalzustands haben. Die elektronischen Bearbei-
tungsvorgänge sind zu protokollieren und mit dem elektronischen Dokument zu 
speichern, damit die Nachvollziehbarkeit und Prüfbarkeit des Originalzustands und 
seiner Ergänzungen gewährleistet ist. 
124 
Hinsichtlich der Aufbewahrung digitaler Unterlagen bei Bargeschäften wird auf das 
BMF-Schreiben vom 26. November 2010 (IV A 4 - S 0316/08/10004-07, BStBl I 
S. 1342) hingewiesen. 
9.1 Maschinelle Auswertbarkeit (§ 147 Absatz 2 Nummer 2 AO) 
125 
Art und Umfang der maschinellen Auswertbarkeit sind nach den tatsächlichen 
Informations- und Dokumentationsmöglichkeiten zu beurteilen. 
Beispiel 10: 
Datenformat für elektronische Rechnungen ZUGFeRD (Zentraler User Guide des 
Forums elektronische Rechnung Deutschland) 
Hier ist vorgesehen, dass Rechnungen im PDF/A-3-Format versendet werden. Diese 
bestehen aus einem Rechnungsbild (dem augenlesbaren, sichtbaren Teil der PDF-
Datei) und den in die PDF-Datei eingebetteten Rechnungsdaten im standardisierten 
XML-Format. 
Entscheidend ist hier jetzt nicht, ob der Rechnungsempfänger nur das Rechnungsbild 
(Image) nutzt, sondern, dass auch noch tatsächlich XML-Daten vorhanden sind, die 
nicht durch eine Formatumwandlung (z. B. in TIFF) gelöscht werden dürfen.  
Die maschinelle Auswertbarkeit bezieht sich auf sämtliche Inhalte der PDF/A-3-Datei. 
126 
Eine maschinelle Auswertbarkeit ist nach diesem Beurteilungsmaßstab bei aufzeich-
nungs- und aufbewahrungspflichtigen Daten, Datensätzen, elektronischen Dokumen-
ten und elektronischen Unterlagen (vgl. Rzn. 3 bis 5) u. a. gegeben, die 
• mathematisch-technische Auswertungen ermöglichen, 
• eine Volltextsuche ermöglichen, 
• auch ohne mathematisch-technische Auswertungen eine Prüfung im weitesten 


--- Page 31 ---
 
Seite 31
Sinne ermöglichen (z. B. Bildschirmabfragen, die Nachverfolgung von 
Verknüpfungen und Verlinkungen oder die Textsuche nach bestimmten 
Eingabekriterien).  
127 
Mathematisch-technische Auswertung bedeutet, dass alle in den aufzeichnungs- und 
aufbewahrungspflichtigen Daten, Datensätzen, elektronischen Dokumenten und 
elektronischen Unterlagen (vgl. Rzn. 3 bis 5) enthaltenen Informationen automatisiert 
(DV-gestützt) interpretiert, dargestellt, verarbeitet sowie für andere Datenbank-
anwendungen und eingesetzte Prüfsoftware direkt, ohne weitere Konvertierungs- und 
Bearbeitungsschritte und ohne Informationsverlust nutzbar gemacht werden können 
(z. B. für wahlfreie Sortier-, Summier-, Verbindungs- und Filterungsmöglichkeiten). 
Mathematisch-technische Auswertungen sind z. B. möglich bei: 
• Elektronischen Grund(buch)aufzeichnungen (z. B. Kassendaten, Daten aus Waren-
wirtschaftssystem, Inventurlisten), 
• Journaldaten aus Finanzbuchhaltung oder Lohnbuchhaltung, 
• Textdateien oder Dateien aus Tabellenkalkulationen mit strukturierten Daten in 
tabellarischer Form (z. B. Reisekostenabrechnung, Überstundennachweise). 
128 
Neben den Daten in Form von Datensätzen und den elektronischen Dokumenten sind 
auch alle zur maschinellen Auswertung der Daten im Rahmen des Datenzugriffs not-
wendigen Strukturinformationen (z. B. über die Dateiherkunft [eingesetztes System], 
die Dateistruktur, die Datenfelder, verwendete Zeichensatztabellen) in maschinell 
auswertbarer Form sowie die internen und externen Verknüpfungen vollständig und in 
unverdichteter, maschinell auswertbarer Form aufzubewahren. Im Rahmen einer 
Datenträgerüberlassung ist der Erhalt technischer Verlinkungen auf dem Datenträger 
nicht erforderlich, sofern dies nicht möglich ist. 
129 
Die Reduzierung einer bereits bestehenden maschinellen Auswertbarkeit, beispiels-
weise durch Umwandlung des Dateiformats oder der Auswahl bestimmter Aufbewah-
rungsformen, ist nicht zulässig (siehe unter 9.2). 
Beispiele 11: 
• Umwandlung von PDF/A-Dateien ab der Norm PDF/A-3 in ein Bildformat (z. B. 
TIFF, JPEG etc.), da dann die in den PDF/A-Dateien enthaltenen XML-Daten und 
ggf. auch vorhandene Volltextinformationen gelöscht werden. 
• Umwandlung von elektronischen Grund(buch)aufzeichnungen (z. B. Kasse, 
Warenwirtschaft) in ein PDF-Format. 
• Umwandlung von Journaldaten einer Finanzbuchhaltung oder Lohnbuchhaltung in 
ein PDF-Format. 
Eine Umwandlung in ein anderes Format (z. B. Inhouse-Format) ist zulässig, wenn die 
maschinelle Auswertbarkeit nicht eingeschränkt wird und keine inhaltliche Verände-
rung vorgenommen wird (siehe Rz. 135). 


--- Page 32 ---
 
Seite 32
Der Steuerpflichtige muss dabei auch berücksichtigen, dass entsprechende Einschrän-
kungen in diesen Fällen zu seinen Lasten gehen können (z. B. Speicherung einer 
E-Mail als PDF-Datei. Die Informationen des Headers [z. B. Informationen zum 
Absender] gehen dabei verloren und es ist nicht mehr nachvollziehbar, wie der tat-
sächliche Zugang der E-Mail erfolgt ist). 
9.2 Elektronische Aufbewahrung 
130 
Werden Handels- oder Geschäftsbriefe und Buchungsbelege in Papierform empfangen 
und danach elektronisch bildlich erfasst (z. B. gescannt oder fotografiert), ist das 
hierdurch entstandene elektronische Dokument so aufzubewahren, dass die Wieder-
gabe mit dem Original bildlich übereinstimmt, wenn es lesbar gemacht wird (§ 147 
Absatz  2 AO). Eine bildliche Erfassung kann hierbei mit den verschiedensten Arten 
von Geräten (z. B. Smartphones, Multifunktionsgeräten oder Scan-Straßen) erfolgen, 
wenn die Anforderungen dieses Schreibens erfüllt sind. Werden bildlich erfasste 
Dokumente per Optical-Character-Recognition-Verfahren (OCR-Verfahren) um 
Volltextinformationen angereichert (zum Beispiel volltextrecherchierbare PDFs), so 
ist dieser Volltext nach Verifikation und Korrektur über die Dauer der Aufbewah-
rungsfrist aufzubewahren und auch für Prüfzwecke verfügbar zu machen. § 146 
Absatz 2 AO steht einer bildlichen Erfassung durch mobile Geräte (z. B. Smartphones) 
im Ausland nicht entgegen, wenn die Belege im Ausland entstanden sind bzw. 
empfangen wurden und dort direkt erfasst werden (z. B. bei Belegen über eine 
Dienstreise im Ausland). 
131 
Eingehende elektronische Handels- oder Geschäftsbriefe und Buchungsbelege müssen 
in dem Format aufbewahrt werden, in dem sie empfangen wurden (z. B. Rechnungen 
oder Kontoauszüge im PDF- oder Bildformat). Eine Umwandlung in ein anderes 
Format (z. B. MSG in PDF) ist dann zulässig, wenn die maschinelle Auswertbarkeit 
nicht eingeschränkt wird und keine inhaltlichen Veränderungen vorgenommen werden 
(siehe Rz. 135). Erfolgt eine Anreicherung der Bildinformationen, z. B. durch OCR 
(Beispiel: Erzeugung einer volltextrecherchierbaren PDF-Datei im Erfassungsprozess), 
sind die dadurch gewonnenen Informationen nach Verifikation und Korrektur 
ebenfalls aufzubewahren. 
132 
Im DV-System erzeugte Daten im Sinne der Rzn. 3 bis 5 (z. B. Grund(buch)aufzeich-
nungen in Vor- und Nebensystemen, Buchungen, generierte Datensätze zur Erstellung 
von Ausgangsrechnungen) oder darin empfangene Daten (z. B. EDI-Verfahren) 
müssen im Ursprungsformat aufbewahrt werden. 
133 
Im DV-System erzeugte Dokumente (z. B. als Textdokumente erstellte Ausgangs-
rechnungen [§ 14b UStG], elektronisch abgeschlossene Verträge, Handels- und 
Geschäftsbriefe, Verfahrensdokumentation) sind im Ursprungsformat aufzubewahren. 


--- Page 33 ---
 
Seite 33
Unter Zumutbarkeitsgesichtspunkten ist es nicht zu beanstanden, wenn der Steuer-
pflichtige elektronisch erstellte und in Papierform abgesandte Handels- und Geschäfts-
briefe nur in Papierform aufbewahrt (Hinweis auf Rzn. 119, 120). Eine Umwandlung 
in ein anderes Format (z. B. Inhouse-Format) ist zulässig, wenn die maschinelle Aus-
wertbarkeit nicht eingeschränkt wird und keine inhaltliche Veränderung vorgenommen 
wird (siehe Rz. 135).  
134 
Bei Einsatz von Kryptografietechniken ist sicherzustellen, dass die verschlüsselten 
Unterlagen im DV-System in entschlüsselter Form zur Verfügung stehen.  
Werden Signaturprüfschlüssel verwendet, sind die eingesetzten Schlüssel aufzu-
bewahren. Die Aufbewahrungspflicht endet, wenn keine der mit den Schlüsseln 
signierten Unterlagen mehr aufbewahrt werden müssen. 
135 
Bei Umwandlung (Konvertierung) aufbewahrungspflichtiger Unterlagen in ein unter-
nehmenseigenes Format (sog. Inhouse-Format) sind beide Versionen zu archivieren, 
derselben Aufzeichnung zuzuordnen und mit demselben Index zu verwalten sowie die 
konvertierte Version als solche zu kennzeichnen.  
Die Aufbewahrung beider Versionen ist bei Beachtung folgender Anforderungen nicht 
erforderlich, sondern es ist die Aufbewahrung der konvertierten Fassung ausreichend: 
• Es wird keine bildliche oder inhaltliche Veränderung vorgenommen. 
• Bei der Konvertierung gehen keine sonstigen aufbewahrungspflichtigen 
Informationen verloren. 
• Die ordnungsgemäße und verlustfreie Konvertierung wird dokumentiert 
(Verfahrensdokumentation). 
• Die maschinelle Auswertbarkeit und der Datenzugriff durch die Finanzbehörde 
werden nicht eingeschränkt; dabei ist es zulässig, wenn bei der Konvertierung 
Zwischenaggregationsstufen nicht gespeichert, aber in der Verfahrensdokumen-
tation so dargestellt werden, dass die retrograde und progressive Prüfbarkeit 
sichergestellt ist. 
 
Nicht aufbewahrungspflichtig sind die während der maschinellen Verarbeitung durch 
das Buchführungssystem erzeugten Dateien, sofern diese ausschließlich einer 
temporären Zwischenspeicherung von Verarbeitungsergebnissen dienen und deren 
Inhalte im Laufe des weiteren Verarbeitungsprozesses vollständig Eingang in die 
Buchführungsdaten finden. Voraussetzung ist jedoch, dass bei der weiteren Verarbei-
tung keinerlei „Verdichtung“ aufzeichnungs- und aufbewahrungspflichtiger Daten 
(vgl. Rzn. 3 bis 5) vorgenommen wird. 


--- Page 34 ---
 
Seite 34
9.3 Bildliche Erfassung von Papierdokumenten  
136 
Papierdokumente werden durch die bildliche Erfassung (siehe Rz. 130) in elektroni-
sche Dokumente umgewandelt. Das Verfahren muss dokumentiert werden.  
Der Steuerpflichtige sollte daher eine Organisationsanweisung erstellen, die unter 
anderem regelt: 
• wer erfassen darf, 
• zu welchem Zeitpunkt erfasst wird oder erfasst werden soll (z. B. beim 
Posteingang, während oder nach Abschluss der Vorgangsbearbeitung), 
• welches Schriftgut erfasst wird, 
• ob eine bildliche oder inhaltliche Übereinstimmung mit dem Original erforderlich ist,  
• wie die Qualitätskontrolle auf Lesbarkeit und Vollständigkeit und 
• wie die Protokollierung von Fehlern zu erfolgen hat. 
 
Die konkrete Ausgestaltung dieser Verfahrensdokumentation ist abhängig von der 
Komplexität und Diversifikation der Geschäftstätigkeit und der Organisationsstruktur 
sowie des eingesetzten DV-Systems.  
Aus Vereinfachungsgründen (z. B. bei Belegen über eine Dienstreise im Ausland) 
steht § 146 Absatz 2 AO einer bildlichen Erfassung durch mobile Geräte (z. B. 
Smartphones) im Ausland nicht entgegen, wenn die Belege im Ausland entstanden 
sind bzw. empfangen wurden und dort direkt erfasst werden.  
Erfolgt im Zusammenhang mit einer, nach § 146 Absatz 2a AO genehmigten, 
Verlagerung der elektronischen Buchführung ins Ausland eine ersetzende bildliche 
Erfassung, wird es nicht beanstandet, wenn die papierenen Ursprungsbelege zu diesem 
Zweck an den Ort der elektronischen Buchführung verbracht werden. Die bildliche 
Erfassung hat zeitnah zur Verbringung der Papierbelege ins Ausland zu erfolgen. 
 
137 
Eine vollständige Farbwiedergabe ist erforderlich, wenn der Farbe Beweisfunktion 
zukommt (z. B. Minusbeträge in roter Schrift, Sicht-, Bearbeitungs- und Zeichnungs-
vermerke in unterschiedlichen Farben). 
 
138 
Für Besteuerungszwecke ist eine elektronische Signatur oder ein Zeitstempel nicht 
erforderlich.  
139 
Im Anschluss an den Erfassungsvorgang (siehe Rz. 130) darf die weitere Bearbeitung 
nur mit dem elektronischen Dokument erfolgen. Die Papierbelege sind dem weiteren 
Bearbeitungsgang zu entziehen, damit auf diesen keine Bemerkungen, Ergänzungen 
usw. vermerkt werden können, die auf dem elektronischen Dokument nicht enthalten 
sind. Sofern aus organisatorischen Gründen nach dem Erfassungsvorgang eine weitere 
Vorgangsbearbeitung des Papierbeleges erfolgt, muss nach Abschluss der Bearbeitung 


--- Page 35 ---
 
Seite 35
der bearbeitete Papierbeleg erneut erfasst und ein Bezug zur ersten elektronischen 
Fassung des Dokuments hergestellt werden (gemeinsamer Index).  
140 
Nach der bildlichen Erfassung im Sinne der Rz. 130 dürfen Papierdokumente vernich-
tet werden, soweit sie nicht nach außersteuerlichen oder steuerlichen Vorschriften im 
Original aufzubewahren sind. Der Steuerpflichtige muss entscheiden, ob Dokumente, 
deren Beweiskraft bei der Aufbewahrung in elektronischer Form nicht erhalten bleibt, 
zusätzlich in der Originalform aufbewahrt werden sollen.  
141 
Der Verzicht auf einen Papierbeleg darf die Möglichkeit der Nachvollziehbarkeit und 
Nachprüfbarkeit nicht beeinträchtigen. 
9.4 Auslagerung von Daten aus dem Produktivsystem und Systemwechsel 
142 
Im Falle eines Systemwechsels (z. B. Abschaltung Altsystem, Datenmigration), einer 
Systemänderung (z. B. Änderung der OCR-Software, Update der Finanzbuchhaltung 
etc.) oder einer Auslagerung von aufzeichnungs- und aufbewahrungspflichtigen Daten 
(vgl. Rzn. 3 bis 5) aus dem Produktivsystem ist es nur dann nicht erforderlich, die 
ursprüngliche Hard- und Software des Produktivsystems über die Dauer der Aufbe-
wahrungsfrist vorzuhalten, wenn die folgenden Voraussetzungen erfüllt sind: 
1. Die aufzeichnungs- und aufbewahrungspflichtigen Daten (einschließlich 
Metadaten, Stammdaten, Bewegungsdaten und der erforderlichen 
Verknüpfungen) müssen unter Beachtung der Ordnungsvorschriften (vgl. 
§§ 145 bis 147 AO) quantitativ und qualitativ gleichwertig in ein neues System, 
in eine neue Datenbank, in ein Archivsystem oder in ein anderes System 
überführt werden.  
Bei einer erforderlichen Datenumwandlung (Migration) darf ausschließlich das 
Format der Daten (z. B. Datums- und Währungsformat) umgesetzt, nicht aber 
eine inhaltliche Änderung der Daten vorgenommen werden. Die vorgenomme-
nen Änderungen sind zu dokumentieren.  
Die Reorganisation von OCR-Datenbanken ist zulässig, soweit die zugrunde 
liegenden elektronischen Dokumente und Unterlagen durch diesen Vorgang 
unverändert bleiben und die durch das OCR-Verfahren gewonnenen 
Informationen mindestens in quantitativer und qualitativer Hinsicht erhalten 
bleiben. 
1. Das neue System, das Archivsystem oder das andere System muss in quantitati-
ver und qualitativer Hinsicht die gleichen Auswertungen der aufzeichnungs- 
und aufbewahrungspflichtigen Daten ermöglichen als wären die Daten noch im 
Produktivsystem. 
 


--- Page 36 ---
 
Seite 36
143 
Andernfalls ist die ursprüngliche Hard- und Software des Produktivsystems - neben 
den aufzeichnungs- und aufbewahrungspflichtigen Daten - für die Dauer der Aufbe-
wahrungsfrist vorzuhalten. Auf die Möglichkeit der Bewilligung von Erleichterungen 
nach § 148 AO wird hingewiesen. 
144 
Eine Aufbewahrung in Form von Datenextrakten, Reports oder Druckdateien ist 
unzulässig, soweit nicht mehr alle aufzeichnungs- und aufbewahrungspflichtigen 
Daten übernommen werden. 
10. Nachvollziehbarkeit und Nachprüfbarkeit 
145 
Die allgemeinen Grundsätze der Nachvollziehbarkeit und Nachprüfbarkeit sind unter 
3.1 aufgeführt.  
Die Prüfbarkeit der formellen und sachlichen Richtigkeit bezieht sich sowohl auf 
einzelne Geschäftsvorfälle (Einzelprüfung) als auch auf die Prüfbarkeit des gesamten 
Verfahrens (Verfahrens- oder Systemprüfung anhand einer Verfahrensdokumentation, 
siehe unter 10.1).  
146 
Auch an die DV-gestützte Buchführung wird die Anforderung gestellt, dass Geschäfts-
vorfälle für die Dauer der Aufbewahrungsfrist retrograd und progressiv prüfbar 
bleiben müssen.  
147 
Die vorgenannten Anforderungen gelten für sonst erforderliche elektronische Auf-
zeichnungen sinngemäß (§ 145 Absatz 2 AO).  
148 
Von einem sachverständigen Dritten kann zwar Sachverstand hinsichtlich der 
Ordnungsvorschriften der §§ 145 bis 147 AO und allgemeiner DV-Sachverstand 
erwartet werden, nicht jedoch spezielle, produktabhängige System- oder Programmier-
kenntnisse.  
149 
Nach § 146 Absatz 3 Satz 3 AO muss im Einzelfall die Bedeutung von Abkürzungen, 
Ziffern, Buchstaben und Symbolen eindeutig festliegen und sich aus der Verfahrens-
dokumentation ergeben.  
150 
Für die Prüfung ist eine aussagefähige und aktuelle Verfahrensdokumentation 
notwendig, die alle System- bzw. Verfahrensänderungen inhaltlich und zeitlich 
lückenlos dokumentiert. 
10.1 
Verfahrensdokumentation 
151 
Da sich die Ordnungsmäßigkeit neben den elektronischen Büchern und sonst erforder-
lichen Aufzeichnungen auch auf die damit in Zusammenhang stehenden Verfahren 
und Bereiche des DV-Systems bezieht (siehe unter 3.), muss für jedes DV-System eine 
übersichtlich gegliederte Verfahrensdokumentation vorhanden sein, aus der Inhalt, 


--- Page 37 ---
 
Seite 37
Aufbau, Ablauf und Ergebnisse des DV-Verfahrens vollständig und schlüssig ersicht-
lich sind. Der Umfang der im Einzelfall erforderlichen Dokumentation wird dadurch 
bestimmt, was zum Verständnis des DV-Verfahrens, der Bücher und Aufzeichnungen 
sowie der aufbewahrten Unterlagen notwendig ist. Die Verfahrensdokumentation muss 
verständlich und damit für einen sachverständigen Dritten in angemessener Zeit nach-
prüfbar sein. Die konkrete Ausgestaltung der Verfahrensdokumentation ist abhängig 
von der Komplexität und Diversifikation der Geschäftstätigkeit und der Organisations-
struktur sowie des eingesetzten DV-Systems.  
152 
Die Verfahrensdokumentation beschreibt den organisatorisch und technisch gewollten 
Prozess, z. B. bei elektronischen Dokumenten von der Entstehung der Informationen 
über die Indizierung, Verarbeitung und Speicherung, dem eindeutigen Wiederfinden 
und der maschinellen Auswertbarkeit, der Absicherung gegen Verlust und Verfäl-
schung und der Reproduktion.  
153 
Die Verfahrensdokumentation besteht in der Regel aus einer allgemeinen Beschrei-
bung, einer Anwenderdokumentation, einer technischen Systemdokumentation und 
einer Betriebsdokumentation.  
154 
Für den Zeitraum der Aufbewahrungsfrist muss gewährleistet und nachgewiesen sein, 
dass das in der Dokumentation beschriebene Verfahren dem in der Praxis eingesetzten 
Verfahren voll entspricht. Dies gilt insbesondere für die eingesetzten Versionen der 
Programme (Programmidentität). Änderungen einer Verfahrensdokumentation müssen 
historisch nachvollziehbar sein. Dem wird genügt, wenn die Änderungen versioniert 
sind und eine nachvollziehbare Änderungshistorie vorgehalten wird. Aus der Verfah-
rensdokumentation muss sich ergeben, wie die Ordnungsvorschriften (z. B. §§ 145 ff. 
AO, §§ 238 ff. HGB) und damit die in diesem Schreiben enthaltenen Anforderungen 
beachtet werden. Die Aufbewahrungsfrist für die Verfahrensdokumentation läuft nicht 
ab, soweit und solange die Aufbewahrungsfrist für die Unterlagen noch nicht abgelaufen 
ist, zu deren Verständnis sie erforderlich ist.  
155 
Soweit eine fehlende oder ungenügende Verfahrensdokumentation die Nachvoll-
ziehbarkeit und Nachprüfbarkeit nicht beeinträchtigt, liegt kein formeller Mangel mit 
sachlichem Gewicht vor, der zum Verwerfen der Buchführung führen kann. 
10.2 
Lesbarmachung von elektronischen Unterlagen 
156 
Wer aufzubewahrende Unterlagen in der Form einer Wiedergabe auf einem Bildträger 
oder auf anderen Datenträgern vorlegt, ist nach § 147 Absatz 5 AO verpflichtet, auf 
seine Kosten diejenigen Hilfsmittel zur Verfügung zu stellen, die erforderlich sind, um 
die Unterlagen lesbar zu machen. Auf Verlangen der Finanzbehörde hat der Steuer-
pflichtige auf seine Kosten die Unterlagen unverzüglich ganz oder teilweise auszu-
drucken oder ohne Hilfsmittel lesbare Reproduktionen beizubringen. 


--- Page 38 ---
 
Seite 38
157 
Der Steuerpflichtige muss durch Erfassen im Sinne der Rz. 130 digitalisierte Unter-
lagen über sein DV-System per Bildschirm lesbar machen. Ein Ausdruck auf Papier ist 
nicht ausreichend. Die elektronischen Dokumente müssen für die Dauer der Aufbe-
wahrungsfrist jederzeit lesbar sein (BFH-Beschluss vom 26. September 2007, BStBl II 
2008 S. 415). 
11. Datenzugriff 
158 
Die Finanzbehörde hat das Recht, die mit Hilfe eines DV-Systems erstellten und nach 
§ 147 Absatz 1 AO aufbewahrungspflichtigen Unterlagen durch Datenzugriff zu 
prüfen. Das Recht auf Datenzugriff steht der Finanzbehörde nur im Rahmen der 
gesetzlichen Regelungen zu (z.B. Außenprüfung und Nachschauen). Durch die 
Regelungen zum Datenzugriff wird der sachliche Umfang der Außenprüfung (§ 194 
AO) nicht erweitert; er wird durch die Prüfungsanordnung (§ 196 AO, § 5 BpO) 
bestimmt.  
11.1 
Umfang und Ausübung des Rechts auf Datenzugriff nach § 147 Absatz 6 
AO 
159 
Gegenstand der Prüfung sind die nach außersteuerlichen und steuerlichen Vorschriften 
aufzeichnungspflichtigen und die nach § 147 Absatz 1 AO aufbewahrungspflichtigen 
Unterlagen. Hierfür sind insbesondere die Daten der Finanzbuchhaltung, der Anlagen-
buchhaltung, der Lohnbuchhaltung und aller Vor- und Nebensysteme, die aufzeich-
nungs- und aufbewahrungspflichtige Unterlagen enthalten (vgl. Rzn. 3 bis 5), für den 
Datenzugriff bereitzustellen. Die Art der Außenprüfung ist hierbei unerheblich, so 
dass z. B. die Daten der Finanzbuchhaltung auch Gegenstand der Lohnsteuer-Außen-
prüfung sein können. 
160 
Neben den Daten müssen insbesondere auch die Teile der Verfahrensdokumentation 
auf Verlangen zur Verfügung gestellt werden können, die einen vollständigen 
Systemüberblick ermöglichen und für das Verständnis des DV-Systems erforderlich 
sind. Dazu gehört auch ein Überblick über alle im DV-System vorhandenen Informa-
tionen, die aufzeichnungs- und aufbewahrungspflichtige Unterlagen betreffen (vgl. 
Rzn. 3 bis 5); z. B. Beschreibungen zu Tabellen, Feldern, Verknüpfungen und 
Auswertungen. Diese Angaben sind erforderlich, damit die Finanzverwaltung das 
durch den Steuerpflichtigen ausgeübte Erstqualifikationsrecht (vgl. Rz. 161) prüfen 
und Aufbereitungen für die Datenträgerüberlassung erstellen kann. 
161 
Soweit in Bereichen des Unternehmens betriebliche Abläufe mit Hilfe eines DV-
Systems abgebildet werden, sind die betroffenen DV-Systeme durch den Steuer-
pflichtigen zu identifizieren, die darin enthaltenen Daten nach Maßgabe der außer-
steuerlichen und steuerlichen Aufzeichnungs- und Aufbewahrungspflichten 


--- Page 39 ---
 
Seite 39
(vgl. Rzn. 3 bis 5) zu qualifizieren (Erstqualifizierung) und für den Datenzugriff in 
geeigneter Weise vorzuhalten (siehe auch unter 9.4). Bei unzutreffender Qualifi-
zierung von Daten kann die Finanzbehörde im Rahmen ihres pflichtgemäßen 
Ermessens verlangen, dass der Steuerpflichtige den Datenzugriff auf diese nach 
außersteuerlichen und steuerlichen Vorschriften tatsächlich aufgezeichneten und 
aufbewahrten Daten nachträglich ermöglicht.  
Beispiele 12: 
• Ein Steuerpflichtiger stellt aus dem PC-Kassensystem nur Tagesendsummen zur 
Verfügung. Die digitalen Grund(buch)aufzeichnungen (Kasseneinzeldaten) wur-
den archiviert, aber nicht zur Verfügung gestellt. 
• Ein Steuerpflichtiger stellt für die Datenträgerüberlassung nur einzelne Sachkonten 
aus der Finanzbuchhaltung zur Verfügung. Die Daten der Finanzbuchhaltung sind 
archiviert. 
• Ein Steuerpflichtiger ohne Auskunftsverweigerungsrecht stellt Belege in Papier-
form zur Verfügung. Die empfangenen und abgesandten Handels- und Geschäfts-
briefe und Buchungsbelege stehen in einem Dokumenten-Management-System zur 
Verfügung. 
162 
Das allgemeine Auskunftsrecht des Prüfers (§§ 88, 199 Absatz 1 AO) und die 
Mitwirkungspflichten des Steuerpflichtigen (§§ 90, 200 AO) bleiben unberührt. 
163 
Bei der Ausübung des Rechts auf Datenzugriff stehen der Finanzbehörde nach dem 
Gesetz drei gleichberechtigte Möglichkeiten zur Verfügung.  
164 
Die Entscheidung, von welcher Möglichkeit des Datenzugriffs die Finanzbehörde 
Gebrauch macht, steht in ihrem pflichtgemäßen Ermessen; falls erforderlich, kann sie 
auch kumulativ mehrere Möglichkeiten in Anspruch nehmen (Rzn. 165 bis 170). 
Sofern noch nicht mit der Außenprüfung begonnen wurde, ist es im Falle eines 
Systemwechsels oder einer Auslagerung von aufzeichnungs- und aufbewahrungs-
pflichtigen Daten aus dem Produktivsystem ausreichend, wenn nach Ablauf des 
5. Kalenderjahres, das auf die Umstellung folgt, nur noch der Z3-Zugriff (Rzn. 167 bis 
170) zur Verfügung gestellt wird. 
 
165 
Unmittelbarer Datenzugriff (Z1) 
Die Finanzbehörde hat das Recht, selbst unmittelbar auf das DV-System dergestalt 
zuzugreifen, dass sie in Form des Nur-Lesezugriffs Einsicht in die aufzeichnungs- und 
aufbewahrungspflichtigen Daten nimmt und die vom Steuerpflichtigen oder von einem 
beauftragten Dritten eingesetzte Hard- und Software zur Prüfung der gespeicherten 
Daten einschließlich der jeweiligen Meta-, Stamm- und Bewegungsdaten sowie der 
entsprechenden Verknüpfungen (z. B. zwischen den Tabellen einer relationalen 
Datenbank) nutzt.  


--- Page 40 ---
 
Seite 40
Dabei darf sie nur mit Hilfe dieser Hard- und Software auf die elektronisch gespei-
cherten Daten zugreifen. Dies schließt eine Fernabfrage (Online-Zugriff) der 
Finanzbehörde auf das DV-System des Steuerpflichtigen durch die Finanzbehörde aus. 
Der Nur-Lesezugriff umfasst das Lesen und Analysieren der Daten unter Nutzung der 
im DV-System vorhandenen Auswertungsmöglichkeiten (z. B. Filtern und Sortieren). 
166 
Mittelbarer Datenzugriff (Z2) 
Die Finanzbehörde kann vom Steuerpflichtigen auch verlangen, dass er an ihrer Stelle 
die aufzeichnungs- und aufbewahrungspflichtigen Daten nach ihren Vorgaben 
maschinell auswertet oder von einem beauftragten Dritten maschinell auswerten lässt, 
um anschließend einen Nur-Lesezugriff durchführen zu können. Es kann nur eine 
maschinelle Auswertung unter Verwendung der im DV-System des Steuerpflichtigen 
oder des beauftragten Dritten vorhandenen Auswertungsmöglichkeiten verlangt 
werden. 
167 
Datenträgerüberlassung (Z3) 
Die Finanzbehörde kann ferner verlangen, dass ihr die aufzeichnungs- und aufbewah-
rungspflichtigen Daten, einschließlich der jeweiligen Meta-, Stamm- und Bewegungs-
daten sowie der internen und externen Verknüpfungen (z. B. zwischen den Tabellen 
einer relationalen Datenbank), und elektronische Dokumente und Unterlagen auf 
einem maschinell lesbaren und auswertbaren Datenträger zur Auswertung überlassen 
werden. Die Finanzbehörde ist nicht berechtigt, selbst Daten aus dem DV-System 
herunterzuladen oder Kopien vorhandener Datensicherungen vorzunehmen. 
168 
Die Datenträgerüberlassung umfasst die Mitnahme der Daten aus der Sphäre des 
Steuerpflichtigen. Eine Mitnahme der Datenträger aus der Sphäre des Steuerpflich-
tigen sollte im Regelfall nur in Abstimmung mit dem Steuerpflichtigen erfolgen. 
169 
Der zur Auswertung überlassene Datenträger ist spätestens nach Bestandskraft der 
aufgrund der Außenprüfung ergangenen Bescheide an den Steuerpflichtigen zurück-
zugeben und die Daten sind zu löschen. 
170 
Die Finanzbehörde hat bei Anwendung der Regelungen zum Datenzugriff den Grund-
satz der Verhältnismäßigkeit zu beachten. 
11.2 
Umfang der Mitwirkungspflicht nach §§ 147 Absatz 6 und 200 Absatz 1 
Satz 2 AO 
171 
Der Steuerpflichtige hat die Finanzbehörde bei Ausübung ihres Rechts auf Datenzu-
griff zu unterstützen (§ 200 Absatz 1 AO). Dabei entstehende Kosten hat der Steuer-
pflichtige zu tragen (§ 147 Absatz 6 Satz 3 AO). 
172 
Enthalten elektronisch gespeicherte Datenbestände z. B. nicht aufzeichnungs- und auf-
bewahrungspflichtige, personenbezogene oder dem Berufsgeheimnis (§ 102 AO) 


--- Page 41 ---
 
Seite 41
unterliegende Daten, so obliegt es dem Steuerpflichtigen oder dem von ihm beauftrag-
ten Dritten, die Datenbestände so zu organisieren, dass der Prüfer nur auf die auf-
zeichnungs- und aufbewahrungspflichtigen Daten des Steuerpflichtigen zugreifen 
kann. Dies kann z. B. durch geeignete Zugriffsbeschränkungen oder „digitales 
Schwärzen“ der zu schützenden Informationen erfolgen. Für versehentlich überlassene 
Daten besteht kein Verwertungsverbot. 
173 
Mangels Nachprüfbarkeit akzeptiert die Finanzbehörde keine Reports oder Druck-
dateien, die vom Unternehmen ausgewählte („vorgefilterte“) Datenfelder und -sätze 
aufführen, jedoch nicht mehr alle aufzeichnungs- und aufbewahrungspflichtigen Daten 
(vgl. Rzn. 3 bis 5) enthalten.  
Im Einzelnen gilt Folgendes: 
174 
Beim unmittelbaren Datenzugriff hat der Steuerpflichtige dem Prüfer die für den 
Datenzugriff erforderlichen Hilfsmittel zur Verfügung zu stellen und ihn für den Nur-
Lesezugriff in das DV-System einzuweisen. Die Zugangsberechtigung muss so aus-
gestaltet sein, dass dem Prüfer dieser Zugriff auf alle aufzeichnungs- und aufbewah-
rungspflichtigen Daten eingeräumt wird. Sie umfasst die im DV-System genutzten 
Auswertungsmöglichkeiten (z. B. Filtern, Sortieren, Konsolidieren) für Prüfungs-
zwecke (z. B. in Revisionstools, Standardsoftware, Backofficeprodukten). In Abhän-
gigkeit vom konkreten Sachverhalt kann auch eine vom Steuerpflichtigen nicht 
genutzte, aber im DV-System vorhandene Auswertungsmöglichkeit verlangt werden.  
Eine Volltextsuche, eine Ansichtsfunktion oder ein selbsttragendes System, das in 
einer Datenbank nur die für archivierte Dateien vergebenen Schlagworte als Index-
werte nachweist, reicht regelmäßig nicht aus. 
Eine Unveränderbarkeit des Datenbestandes und des DV-Systems durch die Finanz-
behörde muss seitens des Steuerpflichtigen oder eines von ihm beauftragten Dritten 
gewährleistet werden. 
175 
Beim mittelbaren Datenzugriff gehört zur Mithilfe des Steuerpflichtigen beim Nur-
Lesezugriff neben der Zurverfügungstellung von Hard- und Software die Unter-
stützung durch mit dem DV-System vertraute Personen. Der Umfang der zumutbaren 
Mithilfe richtet sich nach den betrieblichen Gegebenheiten des Unternehmens.  
Hierfür können z. B. seine Größe oder Mitarbeiterzahl Anhaltspunkte sein. 
176 
Bei der Datenträgerüberlassung sind der Finanzbehörde mit den gespeicherten Unter-
lagen und Aufzeichnungen alle zur Auswertung der Daten notwendigen Informationen 
(z. B. über die Dateiherkunft [eingesetztes System], die Dateistruktur, die Datenfelder, 
verwendete Zeichensatztabellen sowie interne und externe Verknüpfungen) in 
maschinell auswertbarer Form zur Verfügung zu stellen. Dies gilt auch in den Fällen, 
in denen sich die Daten bei einem Dritten befinden. 
Auch die zur Auswertung der Daten notwendigen Strukturinformationen müssen in 


--- Page 42 ---
 
Seite 42
maschinell auswertbarer Form zur Verfügung gestellt werden. 
Bei unvollständigen oder unzutreffenden Datenlieferungen kann die Finanzbehörde 
neue Datenträger mit vollständigen und zutreffenden Daten verlangen. Im Verlauf der 
Prüfung kann die Finanzbehörde auch weitere Datenträger mit aufzeichnungs- und 
aufbewahrungspflichtigen Unterlagen anfordern. 
Das Einlesen der Daten muss ohne Installation von Fremdsoftware auf den Rechnern 
der Finanzbehörde möglich sein. Eine Entschlüsselung der übergebenen Daten muss 
spätestens bei der Datenübernahme auf die Systeme der Finanzverwaltung erfolgen. 
177 
Der Grundsatz der Wirtschaftlichkeit rechtfertigt nicht den Einsatz einer Software, die 
den in diesem Schreiben niedergelegten Anforderungen zur Datenträgerüberlassung 
nicht oder nur teilweise genügt und damit den Datenzugriff einschränkt. Die zur Her-
stellung des Datenzugriffs erforderlichen Kosten muss der Steuerpflichtige genauso in 
Kauf nehmen wie alle anderen Aufwendungen, die die Art seines Betriebes mit sich 
bringt. 
178 
Ergänzende Informationen zur Datenträgerüberlassung stehen auf der Internet-Seite 
des Bundesministeriums der Finanzen zum Download bereit. Die Digitale Schnittstelle 
der Finanzverwaltung für Kassensysteme (DSFinV-K) steht auf der Internet-Seite des 
Bundeszentralamts für Steuern (www.bzst.de) zum Download bereit. 
12. Zertifizierung und Software-Testate 
179 
Die Vielzahl und unterschiedliche Ausgestaltung und Kombination der DV-Systeme 
für die Erfüllung außersteuerlicher oder steuerlicher Aufzeichnungs- und Aufbewah-
rungspflichten lassen keine allgemein gültigen Aussagen der Finanzbehörde zur 
Konformität der verwendeten oder geplanten Hard- und Software zu. Dies gilt umso 
mehr, als weitere Kriterien (z. B. Releasewechsel, Updates, die Vergabe von Zugriffs-
rechten oder Parametrisierungen, die Vollständigkeit und Richtigkeit der eingegebenen 
Daten) erheblichen Einfluss auf die Ordnungsmäßigkeit eines DV-Systems und damit 
auf Bücher und die sonst erforderlichen Aufzeichnungen haben können. 
180 
Positivtestate zur Ordnungsmäßigkeit der Buchführung - und damit zur Ordnungs-
mäßigkeit DV-gestützter Buchführungssysteme - werden weder im Rahmen einer 
steuerlichen Außenprüfung noch im Rahmen einer verbindlichen Auskunft erteilt. 
181 
„Zertifikate“ oder „Testate“ Dritter können bei der Auswahl eines Softwareproduktes 
dem Unternehmen als Entscheidungskriterium dienen, entfalten jedoch aus den in 
Rz. 179 genannten Gründen gegenüber der Finanzbehörde keine Bindungswirkung. 
13. Anwendungsregelung 
182 
Im Übrigen bleiben die Regelungen des BMF-Schreibens vom 1. Februar 1984 
(IV A 7 - S 0318-1/84, BStBl I S. 155) unberührt. 


--- Page 43 ---
 
Seite 43
183 
Dieses BMF-Schreiben tritt mit Wirkung vom 1. Januar 2020 an die Stelle des BMF-
Schreibens vom 14. November 2014 - IV A 4 - S 0316/13/10003 -, BStBl I S. 1450.  
184 
Die übrigen Grundsätze dieses Schreibens sind auf Besteuerungszeiträume 
anzuwenden, die nach dem 31. Dezember 2019 beginnen. Es wird nicht beanstandet, 
wenn der Steuerpflichtige diese Grundsätze auf Besteuerungszeiträume anwendet, die 
vor dem 1. Januar 2020 enden. 
 
 
 


--- Page 44 ---
 
Seite 44
Dieses Schreiben wird im Bundessteuerblatt Teil I veröffentlicht. 
 
Im Auftrag 
 
', '99188dfdc452af6c927dba5ff05f9c7a62eefb1bcf5134d205759c6925a29065', NULL, '2026-01-16T19:14:47.797819+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (3, 'document', '/home/ghost/Repositories/Uni-Projekt-Graph-RAG/workspace/job_bad7b675-da31-439f-a32e-e9505c8ab308/documents/GoBD.pdf', 'GoBD.pdf', NULL, '--- Page 1 ---
 
Postanschrift Berlin: Bundesministeriu m der Finanzen, 11016 Berlin  
www.bundesfinanzministerium.de
 
 
 
 
POSTANSCHRIFT
Bundesministerium der Finanzen, 11016 Berlin 
 
Nur per E-Mail 
Oberste Finanzbehörden 
der Länder 
- bp@finmail.de - 
HAUSANSCHRIFT Wilhelmstraße 97 
10117 Berlin 
 
TEL +49 (0) 30 18 682-0 
 
 
 
E-MAIL poststelle@bmf.bund.de 
 
DATUM 28. November 2019 
 
 
 
BETREFF Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, 
Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff 
(GoBD) 
GZ IV A 4 - S 0316/19/10003 :001 
DOK 2019/0962810 
(bei Antwort bitte GZ und DOK angeben) 
 
Unter Bezugnahme auf das Ergebnis der Erörterungen mit den obersten Finanzbehörden der 
Länder gilt für die Anwendung dieser Grundsätze Folgendes: 
 
 


--- Page 2 ---
 
Seite 2
Inhalt 
1. 
ALLGEMEINES .......................................................................................................................................... 4 
1.1 
NUTZBARMACHUNG AUßERSTEUERLICHER BUCHFÜHRUNGS- UND AUFZEICHNUNGSPFLICHTEN FÜR DAS STEUERRECHT 4 
1.2 
STEUERLICHE BUCHFÜHRUNGS- UND AUFZEICHNUNGSPFLICHTEN ....................................................................... 4 
1.3 
AUFBEWAHRUNG VON UNTERLAGEN ZU GESCHÄFTSVORFÄLLEN UND VON SOLCHEN UNTERLAGEN, DIE ZUM 
VERSTÄNDNIS UND ZUR ÜBERPRÜFUNG DER FÜR DIE BESTEUERUNG GESETZLICH VORGESCHRIEBENEN 
AUFZEICHNUNGEN VON BEDEUTUNG SIND ...................................................................................................... 4 
1.4 
ORDNUNGSVORSCHRIFTEN ........................................................................................................................... 5 
1.5 
FÜHRUNG VON BÜCHERN UND SONST ERFORDERLICHEN AUFZEICHNUNGEN AUF DATENTRÄGERN ............................ 5 
1.6 
BEWEISKRAFT VON BUCHFÜHRUNG UND AUFZEICHNUNGEN, DARSTELLUNG VON BEANSTANDUNGEN DURCH DIE 
FINANZVERWALTUNG .................................................................................................................................. 6 
1.7 
AUFZEICHNUNGEN ...................................................................................................................................... 6 
1.8 
BÜCHER .................................................................................................................................................... 7 
1.9 
GESCHÄFTSVORFÄLLE .................................................................................................................................. 7 
1.10 
GRUNDSÄTZE ORDNUNGSMÄßIGER BUCHFÜHRUNG (GOB) ............................................................................... 7 
1.11 
DATENVERARBEITUNGSSYSTEM; HAUPT-, VOR- UND NEBENSYSTEME ................................................................. 8 
2. 
VERANTWORTLICHKEIT ........................................................................................................................... 8 
3. 
ALLGEMEINE ANFORDERUNGEN.............................................................................................................. 8 
3.1 
GRUNDSATZ DER NACHVOLLZIEHBARKEIT UND NACHPRÜFBARKEIT (§ 145 ABSATZ 1 AO, § 238 ABSATZ 1 SATZ 2 
UND SATZ 3 HGB) ................................................................................................................................... 10 
3.2 
GRUNDSÄTZE DER WAHRHEIT, KLARHEIT UND FORTLAUFENDEN AUFZEICHNUNG ................................................. 10 
3.2.1 
Vollständigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) ............................................................. 10 
3.2.2 
Richtigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) .................................................................... 12 
3.2.3 
Zeitgerechte Buchungen und Aufzeichnungen (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB)............ 12 
3.2.4 
Ordnung (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) ....................................................................... 14 
3.2.5 
Unveränderbarkeit (§ 146 Absatz 4 AO, § 239 Absatz 3 HGB) ....................................................... 15 
4. 
BELEGWESEN (BELEGFUNKTION) ............................................................................................................16 
4.1 
BELEGSICHERUNG ..................................................................................................................................... 17 
4.2 
ZUORDNUNG ZWISCHEN BELEG UND GRUND(BUCH)AUFZEICHNUNG ODER BUCHUNG .......................................... 17 
4.3 
ERFASSUNGSGERECHTE AUFBEREITUNG DER BUCHUNGSBELEGE ........................................................................ 18 
4.4 
BESONDERHEITEN ..................................................................................................................................... 21 
5. 
 AUFZEICHNUNG DER GESCHÄFTSVORFÄLLE IN ZEITLICHER REIHENFOLGE UND IN SACHLICHER 
ORDNUNG (GRUND(BUCH)AUFZEICHNUNGEN, JOURNAL- UND KONTENFUNKTION) .............................21 
5.1 
ERFASSUNG IN GRUND(BUCH)AUFZEICHNUNGEN ........................................................................................... 22 
5.2 
DIGITALE GRUND(BUCH)AUFZEICHNUNGEN................................................................................................... 22 
5.3 
VERBUCHUNG IM JOURNAL (JOURNALFUNKTION) .......................................................................................... 23 
5.4 
AUFZEICHNUNG DER GESCHÄFTSVORFÄLLE IN SACHLICHER ORDNUNG (HAUPTBUCH) ........................................... 24 
6. 
INTERNES KONTROLLSYSTEM (IKS) .........................................................................................................25 
7. 
DATENSICHERHEIT ..................................................................................................................................26 
8. 
UNVERÄNDERBARKEIT, PROTOKOLLIERUNG VON ÄNDERUNGEN ..........................................................26 


--- Page 3 ---
 
Seite 3
9.  
     AUFBEWAHRUNG ..............................................................................................................................28 
9.1 
MASCHINELLE AUSWERTBARKEIT (§ 147 ABSATZ 2 NUMMER 2 AO) ............................................................... 30 
9.2 
ELEKTRONISCHE AUFBEWAHRUNG ............................................................................................................... 31 
9.3 
BILDLICHE ERFASSUNG VON PAPIERDOKUMENTEN ......................................................................................... 33 
9.4 
AUSLAGERUNG VON DATEN AUS DEM PRODUKTIVSYSTEM UND SYSTEMWECHSEL ................................................ 34 
10. 
NACHVOLLZIEHBARKEIT UND NACHPRÜFBARKEIT .............................................................................35 
10.1 
VERFAHRENSDOKUMENTATION ................................................................................................................... 36 
10.2 
LESBARMACHUNG VON ELEKTRONISCHEN UNTERLAGEN .................................................................................. 37 
11. 
DATENZUGRIFF ...................................................................................................................................37 
11.1 
UMFANG UND AUSÜBUNG DES RECHTS AUF DATENZUGRIFF NACH § 147 ABSATZ 6 AO ...................................... 38 
11.2 
UMFANG DER MITWIRKUNGSPFLICHT NACH §§ 147 ABSATZ 6 UND 200 ABSATZ 1 SATZ 2 AO ............................ 40 
12. 
ZERTIFIZIERUNG UND SOFTWARE-TESTATE ........................................................................................42 
13. 
ANWENDUNGSREGELUNG .................................................................................................................42 
 
 
 


--- Page 4 ---
 
Seite 4
1. Allgemeines 
1 
 Die betrieblichen Abläufe in den Unternehmen werden ganz oder teilweise unter Ein-
satz von Informations- und Kommunikations-Technik abgebildet. 
2 
Auch die nach außersteuerlichen oder steuerlichen Vorschriften zu führenden Bücher 
und sonst erforderlichen Aufzeichnungen werden in den Unternehmen zunehmend in 
elektronischer Form geführt (z. B. als Datensätze). Darüber hinaus werden in den 
Unternehmen zunehmend die aufbewahrungspflichtigen Unterlagen in elektronischer 
Form (z. B. als elektronische Dokumente) aufbewahrt. 
1.1 Nutzbarmachung außersteuerlicher Buchführungs- und Aufzeichnungs-
pflichten für das Steuerrecht 
3 
Nach § 140 AO sind die außersteuerlichen Buchführungs- und Aufzeichnungspflich-
ten, die für die Besteuerung von Bedeutung sind, auch für das Steuerrecht zu erfüllen. 
Außersteuerliche Buchführungs- und Aufzeichnungspflichten ergeben sich insbeson-
dere aus den Vorschriften der §§ 238 ff. HGB und aus den dort bezeichneten handels-
rechtlichen Grundsätzen ordnungsmäßiger Buchführung (GoB). Für einzelne Rechts-
formen ergeben sich flankierende Aufzeichnungspflichten z. B. aus §§ 91 ff. Aktien-
gesetz, §§ 41 ff. GmbH-Gesetz oder § 33 Genossenschaftsgesetz. Des Weiteren sind 
zahlreiche gewerberechtliche oder branchenspezifische Aufzeichnungsvorschriften 
vorhanden, die gem. § 140 AO im konkreten Einzelfall für die Besteuerung von 
Bedeutung sind, wie z. B. Apothekenbetriebsordnung, Eichordnung, Fahrlehrergesetz, 
Gewerbeordnung, § 26 Kreditwesengesetz oder § 55 Versicherungsaufsichtsgesetz.  
1.2 Steuerliche Buchführungs- und Aufzeichnungspflichten 
4 
 Steuerliche Buchführungs- und Aufzeichnungspflichten ergeben sich sowohl aus der 
Abgabenordnung (z. B. §§ 90 Absatz 3, 141 bis 144 AO) als auch aus Einzelsteuer-
gesetzen (z. B. § 22 UStG, § 4 Absatz 3 Satz 5, § 4 Absatz 4a Satz 6, § 4 Absatz 7 und 
§ 41 EStG). 
1.3 Aufbewahrung von Unterlagen zu Geschäftsvorfällen und von solchen 
Unterlagen, die zum Verständnis und zur Überprüfung der für die Besteue-
rung gesetzlich vorgeschriebenen Aufzeichnungen von Bedeutung sind 
5 
Neben den außersteuerlichen und steuerlichen Büchern, Aufzeichnungen und Unter-
lagen zu Geschäftsvorfällen sind alle Unterlagen aufzubewahren, die zum Verständnis 
und zur Überprüfung der für die Besteuerung gesetzlich vorgeschriebenen Aufzeich-
nungen im Einzelfall von Bedeutung sind (vgl. BFH-Urteil vom 24. Juni 2009, 


--- Page 5 ---
 
Seite 5
BStBl II 2010 S. 452).  
 
Dazu zählen neben Unterlagen in Papierform auch alle Unterlagen in Form von Daten, 
Datensätzen und elektronischen Dokumenten, die dokumentieren, dass die Ordnungs-
vorschriften umgesetzt und deren Einhaltung überwacht wurde. Nicht aufbewahrungs-
pflichtig sind z. B. reine Entwürfe von Handels- oder Geschäftsbriefen, sofern diese 
nicht tatsächlich abgesandt wurden. 
 
Beispiel 1: 
Dienen Kostenstellen der Bewertung von Wirtschaftsgütern, von Rückstellungen oder 
als Grundlage für die Bemessung von Verrechnungspreisen sind diese Aufzeichnun-
gen aufzubewahren, soweit sie zur Erläuterung steuerlicher Sachverhalte benötigt 
werden. 
 
6 
Form, Umfang und Inhalt dieser im Sinne der Rzn. 3 bis 5 nach außersteuerlichen und 
steuerlichen Rechtsnormen aufzeichnungs- und aufbewahrungspflichtigen Unterlagen 
(Daten, Datensätze sowie Dokumente in elektronischer oder Papierform) und der zu 
ihrem Verständnis erforderlichen Unterlagen werden durch den Steuerpflichtigen 
bestimmt. Eine abschließende Definition der aufzeichnungs- und aufbewahrungs-
pflichtigen Aufzeichnungen und Unterlagen ist nicht Gegenstand der nachfolgenden 
Ausführungen. Die Finanzverwaltung kann diese Unterlagen nicht abstrakt im Vorfeld 
für alle Unternehmen abschließend definieren, weil die betrieblichen Abläufe, die auf-
zeichnungs- und aufbewahrungspflichtigen Aufzeichnungen und Unterlagen sowie die 
eingesetzten Buchführungs- und Aufzeichnungssysteme in den Unternehmen zu unter-
schiedlich sind. 
1.4 Ordnungsvorschriften 
7 
Die Ordnungsvorschriften der §§ 145 bis 147 AO gelten für die vorbezeichneten 
Bücher und sonst erforderlichen Aufzeichnungen und der zu ihrem Verständnis 
erforderlichen Unterlagen (vgl. Rzn. 3 bis 5; siehe auch Rzn. 23, 25 und 28). 
1.5 Führung von Büchern und sonst erforderlichen Aufzeichnungen auf 
Datenträgern 
8 
 Bücher und die sonst erforderlichen Aufzeichnungen können nach § 146 Absatz 5 AO 
auch auf Datenträgern geführt werden, soweit diese Form der Buchführung einschließ-
lich des dabei angewandten Verfahrens den GoB entspricht (siehe unter 1.4.). Bei Auf-
zeichnungen, die allein nach den Steuergesetzen vorzunehmen sind, bestimmt sich die 
Zulässigkeit des angewendeten Verfahrens nach dem Zweck, den die Aufzeichnungen 
für die Besteuerung erfüllen sollen (§ 145 Absatz 2 AO; § 146 Absatz 5 Satz 1 2. HS 


--- Page 6 ---
 
Seite 6
AO). Unter diesen Voraussetzungen sind auch Aufzeichnungen auf Datenträgern 
zulässig. 
9 
Somit sind alle Unternehmensbereiche betroffen, in denen betriebliche Abläufe durch 
DV-gestützte Verfahren abgebildet werden und ein Datenverarbeitungssystem (DV-
System, siehe auch Rz. 20) für die Erfüllung der in den Rzn. 3 bis 5 bezeichneten 
außersteuerlichen oder steuerlichen Buchführungs-, Aufzeichnungs- und Aufbewah-
rungspflichten verwendet wird (siehe auch unter 11.1 zum Datenzugriffsrecht). 
10 
Technische Vorgaben oder Standards (z. B. zu Archivierungsmedien oder Kryptogra-
fieverfahren) können angesichts der rasch fortschreitenden Entwicklung und der eben-
falls notwendigen Betrachtung des organisatorischen Umfelds nicht festgelegt werden. 
Im Zweifel ist über einen Analogieschluss festzustellen, ob die Ordnungsvorschriften 
eingehalten wurden, z. B. bei einem Vergleich zwischen handschriftlich geführten 
Handelsbüchern und Unterlagen in Papierform, die in einem verschlossenen Schrank 
aufbewahrt werden, einerseits und elektronischen Handelsbüchern und Unterlagen, die 
mit einem elektronischen Zugriffsschutz gespeichert werden, andererseits. 
1.6 Beweiskraft von Buchführung und Aufzeichnungen, Darstellung von 
Beanstandungen durch die Finanzverwaltung 
11 
Nach § 158 AO sind die Buchführung und die Aufzeichnungen des Steuerpflichtigen, 
die den Vorschriften der §§ 140 bis 148 AO entsprechen, der Besteuerung zugrunde zu 
legen, soweit nach den Umständen des Einzelfalls kein Anlass besteht, ihre sachliche 
Richtigkeit zu beanstanden. Werden Buchführung oder Aufzeichnungen des Steuer-
pflichtigen im Einzelfall durch die Finanzverwaltung beanstandet, so ist durch die 
Finanzverwaltung der Grund der Beanstandung in geeigneter Form darzustellen. 
1.7 Aufzeichnungen 
12 
Aufzeichnungen sind alle dauerhaft verkörperten Erklärungen über Geschäftsvorfälle 
in Schriftform oder auf Medien mit Schriftersatzfunktion (z. B. auf Datenträgern).  
Der Begriff der Aufzeichnungen umfasst Darstellungen in Worten, Zahlen, Symbolen 
und Grafiken.  
13 
Werden Aufzeichnungen nach verschiedenen Rechtsnormen in einer Aufzeichnung 
zusammengefasst (z. B. nach §§ 238 ff. HGB und nach § 22 UStG), müssen die 
zusammengefassten Aufzeichnungen den unterschiedlichen Zwecken genügen. 
Erfordern verschiedene Rechtsnormen gleichartige Aufzeichnungen, so ist eine 
mehrfache Aufzeichnung für jede Rechtsnorm nicht erforderlich. 


--- Page 7 ---
 
Seite 7
1.8 Bücher 
14 
Der Begriff ist funktional unter Anknüpfung an die handelsrechtliche Bedeutung zu 
verstehen. Die äußere Gestalt (gebundenes Buch, Loseblattsammlung oder 
Datenträger) ist unerheblich.  
15 
Der Kaufmann ist verpflichtet, in den Büchern seine Handelsgeschäfte und die Lage 
des Vermögens ersichtlich zu machen (§ 238 Absatz 1 Satz 1 HGB). Der Begriff 
Bücher umfasst sowohl die Handelsbücher der Kaufleute (§§ 238 ff. HGB) als auch 
die diesen entsprechenden Aufzeichnungen von Geschäftsvorfällen der Nichtkauf-
leute. Bei Kleinstunternehmen, die ihren Gewinn durch Einnahmen-Überschussrech-
nung ermitteln (bis 17.500 Euro Jahresumsatz), ist die Erfüllung der Anforderungen an 
die Aufzeichnungen nach den GoBD regelmäßig auch mit Blick auf die Unterneh-
mensgröße zu bewerten. 
1.9 Geschäftsvorfälle 
16 
Geschäftsvorfälle sind alle rechtlichen und wirtschaftlichen Vorgänge, die innerhalb 
eines bestimmten Zeitabschnitts den Gewinn bzw. Verlust oder die Vermögenszusam-
mensetzung in einem Unternehmen dokumentieren oder beeinflussen bzw. verändern 
(z. B. zu einer Veränderung des Anlage- und Umlaufvermögens sowie des Eigen- und 
Fremdkapitals führen). 
1.10 
Grundsätze ordnungsmäßiger Buchführung (GoB) 
17 
Die GoB sind ein unbestimmter Rechtsbegriff, der insbesondere durch Rechtsnormen 
und Rechtsprechung geprägt ist und von der Rechtsprechung und Verwaltung jeweils 
im Einzelnen auszulegen und anzuwenden ist (BFH-Urteil vom 12. Mai 1966, BStBl III 
S. 371; BVerfG-Beschluss vom 10. Oktober 1961, 2 BvL 1/59, BVerfGE 13 S. 153). 
 
18 
Die GoB können sich durch gutachterliche Stellungnahmen, Handelsbrauch, ständige 
Übung, Gewohnheitsrecht, organisatorische und technische Änderungen weiterent-
wickeln und sind einem Wandel unterworfen. 
 
19 
Die GoB enthalten sowohl formelle als auch materielle Anforderungen an eine Buch-
führung. Die formellen Anforderungen ergeben sich insbesondere aus den §§ 238 ff. 
HGB für Kaufleute und aus den §§ 145 bis 147 AO für Buchführungs- und Aufzeich-
nungspflichtige (siehe unter 3.). Materiell ordnungsmäßig sind Bücher und Aufzeich-
nungen, wenn die Geschäftsvorfälle einzeln, nachvollziehbar, vollständig, richtig, zeit-
gerecht und geordnet in ihrer Auswirkung erfasst und anschließend gebucht bzw. ver-
arbeitet sind (vgl. § 239 Absatz 2 HGB, § 145 AO, § 146 Absatz 1 AO). Siehe Rz. 11 
zur Beweiskraft von Buchführung und Aufzeichnungen. 
 


--- Page 8 ---
 
Seite 8
1.11 
Datenverarbeitungssystem; Haupt-, Vor- und Nebensysteme 
20 
Unter DV-System wird die im Unternehmen oder für Unternehmenszwecke zur elek-
tronischen Datenverarbeitung eingesetzte Hard- und Software verstanden, mit denen 
Daten und Dokumente im Sinne der Rzn. 3 bis 5 erfasst, erzeugt, empfangen, über-
nommen, verarbeitet, gespeichert oder übermittelt werden. Dazu gehören das Haupt-
system sowie Vor- und Nebensysteme (z. B. Finanzbuchführungssystem, Anlagen-
buchhaltung, Lohnbuchhaltungssystem, Kassensystem, Warenwirtschaftssystem, 
Zahlungsverkehrssystem, Taxameter, Geldspielgeräte, elektronische Waagen, 
Materialwirtschaft, Fakturierung, Zeiterfassung, Archivsystem, Dokumenten-
Management-System) einschließlich der Schnittstellen zwischen den Systemen. Auf 
die Bezeichnung des DV-Systems oder auf dessen Größe (z. B. Einsatz von Einzel-
geräten oder von Netzwerken) kommt es dabei nicht an. Ebenfalls kommt es nicht 
darauf an, ob die betreffenden DV-Systeme vom Steuerpflichtigen als eigene 
Hardware bzw. Software erworben und genutzt oder in einer Cloud bzw. als eine 
Kombination dieser Systeme betrieben werden. 
2. Verantwortlichkeit 
21 
Für die Ordnungsmäßigkeit elektronischer Bücher und sonst erforderlicher elektroni-
scher Aufzeichnungen im Sinne der Rzn. 3 bis 5, einschließlich der eingesetzten 
Verfahren, ist allein der Steuerpflichtige verantwortlich. Dies gilt auch bei einer 
teilweisen oder vollständigen organisatorischen und technischen Auslagerung von 
Buchführungs- und Aufzeichnungsaufgaben auf Dritte (z. B. Steuerberater oder 
Rechenzentrum).  
3. Allgemeine Anforderungen 
22 
Die Ordnungsmäßigkeit elektronischer Bücher und sonst erforderlicher elektronischer 
Aufzeichnungen im Sinne der Rzn. 3 bis 5 ist nach den gleichen Prinzipien zu beur-
teilen wie die Ordnungsmäßigkeit bei manuell erstellten Büchern oder Aufzeichnun-
gen. 
23 
Das Erfordernis der Ordnungsmäßigkeit erstreckt sich - neben den elektronischen 
Büchern und sonst erforderlichen Aufzeichnungen - auch auf die damit in Zusammen-
hang stehenden Verfahren und Bereiche des DV-Systems (siehe unter 1.11), da die 
Grundlage für die Ordnungsmäßigkeit elektronischer Bücher und sonst erforderlicher 
Aufzeichnungen bereits bei der Entwicklung und Freigabe von Haupt-, Vor- und 
Nebensystemen einschließlich des dabei angewandten DV-gestützten Verfahrens 
gelegt wird. Die Ordnungsmäßigkeit muss bei der Einrichtung und unternehmens-
spezifischen Anpassung des DV-Systems bzw. der DV-gestützten Verfahren im 


--- Page 9 ---
 
Seite 9
konkreten Unternehmensumfeld und für die Dauer der Aufbewahrungsfrist erhalten 
bleiben. 
24 
Die Anforderungen an die Ordnungsmäßigkeit ergeben sich aus: 
• außersteuerlichen Rechtsnormen (z. B. den handelsrechtlichen GoB gem. §§ 238, 
239, 257, 261 HGB), die gem. § 140 AO für das Steuerrecht nutzbar gemacht 
werden können, wenn sie für die Besteuerung von Bedeutung sind, und 
• steuerlichen Ordnungsvorschriften (insbesondere gem. §§ 145 bis 147 AO). 
25 
Die allgemeinen Ordnungsvorschriften in den §§ 145 bis 147 AO gelten nicht nur für 
Buchführungs- und Aufzeichnungspflichten nach § 140 AO und nach den §§ 141 
bis 144 AO. Insbesondere § 145 Absatz 2 AO betrifft alle zu Besteuerungszwecken 
gesetzlich geforderten Aufzeichnungen, also auch solche, zu denen der Steuer-
pflichtige aufgrund anderer Steuergesetze verpflichtet ist, wie z. B. nach § 4 Absatz 3 
Satz 5, Absatz 7 EStG und nach § 22 UStG (BFH-Urteil vom 24. Juni 2009, 
BStBl II 2010 S. 452). 
 
26 
Demnach sind bei der Führung von Büchern in elektronischer oder in Papierform und 
sonst erforderlicher Aufzeichnungen in elektronischer oder in Papierform im Sinne der 
Rzn. 3 bis 5 die folgenden Anforderungen zu beachten: 
• Grundsatz der Nachvollziehbarkeit und Nachprüfbarkeit (siehe unter 3.1), 
• Grundsätze der Wahrheit, Klarheit und fortlaufenden Aufzeichnung (siehe 
unter 3.2): 
 Vollständigkeit (siehe unter 3.2.1), 
 Einzelaufzeichnungspflicht (siehe unter 3.2.1), 
 Richtigkeit (siehe unter 3.2.2), 
 zeitgerechte Buchungen und Aufzeichnungen (siehe unter 3.2.3), 
 Ordnung (siehe unter 3.2.4), 
 Unveränderbarkeit (siehe unter 3.2.5). 
 
27 
Diese Grundsätze müssen während der Dauer der Aufbewahrungsfrist nachweisbar 
erfüllt werden und erhalten bleiben. 
28 
Nach § 146 Absatz 6 AO gelten die Ordnungsvorschriften auch dann, wenn der Unter-
nehmer elektronische Bücher und Aufzeichnungen führt, die für die Besteuerung von 
Bedeutung sind, ohne hierzu verpflichtet zu sein. 
 
29 
Der Grundsatz der Wirtschaftlichkeit rechtfertigt es nicht, dass Grundprinzipien der 
Ordnungsmäßigkeit verletzt und die Zwecke der Buchführung erheblich gefährdet 
werden. Die zur Vermeidung einer solchen Gefährdung erforderlichen Kosten muss 
der Steuerpflichtige genauso in Kauf nehmen wie alle anderen Aufwendungen, die die 
Art seines Betriebes mit sich bringt (BFH-Urteil vom 26. März 1968, BStBl II S. 527). 


--- Page 10 ---
 
Seite 10
3.1 Grundsatz der Nachvollziehbarkeit und Nachprüfbarkeit (§ 145 Absatz 1 
AO, § 238 Absatz 1 Satz 2 und Satz 3 HGB) 
30 
Die Verarbeitung der einzelnen Geschäftsvorfälle sowie das dabei angewandte Buch-
führungs- oder Aufzeichnungsverfahren müssen nachvollziehbar sein. Die Buchungen 
und die sonst erforderlichen Aufzeichnungen müssen durch einen Beleg nachgewiesen 
sein oder nachgewiesen werden können (Belegprinzip, siehe auch unter 4.).  
31 
Aufzeichnungen sind so vorzunehmen, dass der Zweck, den sie für die Besteuerung 
erfüllen sollen, erreicht wird. Damit gelten die nachfolgenden Anforderungen der 
progressiven und retrograden Prüfbarkeit - soweit anwendbar - sinngemäß. 
32 
Die Buchführung muss so beschaffen sein, dass sie einem sachverständigen Dritten 
innerhalb angemessener Zeit einen Überblick über die Geschäftsvorfälle und über die 
Lage des Unternehmens vermitteln kann. Die einzelnen Geschäftsvorfälle müssen sich 
in ihrer Entstehung und Abwicklung lückenlos verfolgen lassen (progressive und 
retrograde Prüfbarkeit). 
 
33 
Die progressive Prüfung beginnt beim Beleg, geht über die Grund(buch)aufzeich-
nungen und Journale zu den Konten, danach zur Bilanz mit Gewinn- und Verlust-
rechnung und schließlich zur Steueranmeldung bzw. Steuererklärung. Die retrograde 
Prüfung verläuft umgekehrt. Die progressive und retrograde Prüfung muss für die 
gesamte Dauer der Aufbewahrungsfrist und in jedem Verfahrensschritt möglich sein.  
34 
Die Nachprüfbarkeit der Bücher und sonst erforderlichen Aufzeichnungen erfordert 
eine aussagekräftige und vollständige Verfahrensdokumentation (siehe unter 10.1), die 
sowohl die aktuellen als auch die historischen Verfahrensinhalte für die Dauer der 
Aufbewahrungsfrist nachweist und den in der Praxis eingesetzten Versionen des DV-
Systems entspricht. 
 
35 
Die Nachvollziehbarkeit und Nachprüfbarkeit muss für die Dauer der Aufbewahrungs-
frist gegeben sein. Dies gilt auch für die zum Verständnis der Buchführung oder Auf-
zeichnungen erforderliche Verfahrensdokumentation. 
3.2 Grundsätze der Wahrheit, Klarheit und fortlaufenden Aufzeichnung 
3.2.1 Vollständigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) 
36 
Die Geschäftsvorfälle sind vollzählig und lückenlos aufzuzeichnen (Grundsatz der 
Einzelaufzeichnungspflicht; vgl. AEAO zu § 146 AO Nr. 2.1). Eine vollzählige und 
lückenlose Aufzeichnung von Geschäftsvorfällen ist auch dann gegeben, wenn 
zulässigerweise nicht alle Datenfelder eines Datensatzes gefüllt werden.  
37 
Die GoB erfordern in der Regel die Aufzeichnung jedes Geschäftsvorfalls - also auch 
jeder Betriebseinnahme und Betriebsausgabe, jeder Einlage und Entnahme - in einem 


--- Page 11 ---
 
Seite 11
Umfang, der eine Überprüfung seiner Grundlagen, seines Inhalts und seiner Bedeu-
tung für den Betrieb ermöglicht. Das bedeutet nicht nur die Aufzeichnung der in Geld 
bestehenden Gegenleistung, sondern auch des Inhalts des Geschäfts und des Namens 
des Vertragspartners (BFH-Urteil vom 12. Mai 1966, BStBl III S. 371) - soweit 
zumutbar, mit ausreichender Bezeichnung des Geschäftsvorfalls (BFH-Urteil vom 
1. Oktober 1969, BStBl 1970 II S. 45). Branchenspezifische Mindestaufzeichnungs-
pflichten und Zumutbarkeitsgesichtspunkte sind zu berücksichtigen. 
Beispiele 2 zu branchenspezifisch entbehrlichen Aufzeichnungen und zur 
Zumutbarkeit: 
• In einem Einzelhandelsgeschäft kommt zulässigerweise eine PC-Kasse ohne Kun-
denverwaltung zum Einsatz. Die Namen der Kunden werden bei Bargeschäften 
nicht erfasst und nicht beigestellt. - Keine Beanstandung. 
• Bei einem Taxiunternehmer werden Angaben zum Kunden im Taxameter nicht 
erfasst und nicht beigestellt. - Keine Beanstandung. 
 
38 
Dies gilt auch für Bareinnahmen; der Umstand der sofortigen Bezahlung rechtfertigt 
keine Ausnahme von diesem Grundsatz (BFH-Urteil vom 26. Februar 2004, BStBl II 
S. 599). 
39 
Die Aufzeichnung jedes einzelnen Geschäftsvorfalls ist nur dann nicht zumutbar, 
wenn es technisch, betriebswirtschaftlich und praktisch unmöglich ist, die einzelnen 
Geschäftsvorfälle aufzuzeichnen (BFH-Urteil vom 12. Mai 1966, IV 472/60, BStBl III 
S. 371). Das Vorliegen dieser Voraussetzungen ist durch den Steuerpflichtigen nach-
zuweisen. 
Beim Verkauf von Waren an eine Vielzahl von nicht bekannten Personen gegen 
Barzahlung gilt die Einzelaufzeichnungspflicht nach § 146 Absatz 1 Satz 1 AO aus 
Zumutbarkeitsgrü nden nicht, wenn kein elektronisches Aufzeichnungssystem, sondern 
eine offene Ladenkasse verwendet wird (§ 146 Absatz 1 Satz 3 und 4 AO, vgl. AEAO 
zu § 146, Nr. 2.1.4). Wird hingegen ein elektronisches Aufzeichnungssystem ver-
wendet, gilt die Einzelaufzeichnungspflicht nach § 146 Absatz 1 Satz 1 AO unab-
hängig davon, ob das elektronische Aufzeichnungssystem und die digitalen Aufzeich-
nungen nach § 146a Absatz 3 AO i. V. m. der KassenSichV mit einer zertifizierten 
technischen Sicherheitseinrichtung zu schü tzen sind. Die Zumutbarkeitsü berlegungen, 
die der Ausnahmeregelung nach § 146 Absatz 1 Satz 3 AO zugrunde liegen, sind 
grundsätzlich auch auf Dienstleistungen ü bertragbar (vgl. AEAO zu § 146, Nr. 2.2.6). 
40 
Die vollständige und lückenlose Erfassung und Wiedergabe aller Geschäftsvorfälle ist 
bei DV-Systemen durch ein Zusammenspiel von technischen (einschließlich program-
mierten) und organisatorischen Kontrollen sicherzustellen (z. B. Erfassungskontrollen, 


--- Page 12 ---
 
Seite 12
Plausibilitätskontrollen bei Dateneingaben, inhaltliche Plausibilitätskontrollen, auto-
matisierte Vergabe von Datensatznummern, Lückenanalyse oder Mehrfachbelegungs-
analyse bei Belegnummern).  
41 
Ein und derselbe Geschäftsvorfall darf nicht mehrfach aufgezeichnet werden. 
Beispiel 3: 
Ein Wareneinkauf wird gewinnwirksam durch Erfassung des zeitgleichen Liefer-
scheins und später nochmals mittels Erfassung der (Sammel)Rechnung erfasst und 
verbucht. Keine mehrfache Aufzeichnung eines Geschäftsvorfalles in verschiedenen 
Systemen oder mit verschiedenen Kennungen (z. B. für Handelsbilanz, für steuerliche 
Zwecke) liegt vor, soweit keine mehrfache bilanzielle oder gewinnwirksame Auswir-
kung gegeben ist.  
42 
Zusammengefasste oder verdichtete Aufzeichnungen im Hauptbuch (Konto) sind 
zulässig, sofern sie nachvollziehbar in ihre Einzelpositionen in den Grund(buch)auf-
zeichnungen oder des Journals aufgegliedert werden können. Andernfalls ist die 
Nachvollziehbarkeit und Nachprüfbarkeit nicht gewährleistet.  
43 
Die Erfassung oder Verarbeitung von tatsächlichen Geschäftsvorfällen darf nicht 
unterdrückt werden. So ist z. B. eine Bon- oder Rechnungserteilung ohne Registrie-
rung der bar vereinnahmten Beträge (Abbruch des Vorgangs) in einem DV-System 
unzulässig. 
3.2.2 Richtigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) 
44 
Geschäftsvorfälle sind in Übereinstimmung mit den tatsächlichen Verhältnissen und 
im Einklang mit den rechtlichen Vorschriften inhaltlich zutreffend durch Belege 
abzubilden (BFH-Urteil vom 24. Juni 1997, BStBl II 1998 S. 51), der Wahrheit ent-
sprechend aufzuzeichnen und bei kontenmäßiger Abbildung zutreffend zu kontieren.  
 
3.2.3 Zeitgerechte Buchungen und Aufzeichnungen (§ 146 Absatz 1 AO, § 239 
Absatz 2 HGB) 
45 
Das Erfordernis „zeitgerecht“ zu buchen verlangt, dass ein zeitlicher Zusammenhang 
zwischen den Vorgängen und ihrer buchmäßigen Erfassung besteht (BFH-Urteil vom 
25. März 1992, BStBl II S. 1010; BFH-Urteil vom 5. März 1965, BStBl III S. 285). 
46 
Jeder Geschäftsvorfall ist zeitnah, d. h. möglichst unmittelbar nach seiner Entstehung in 
einer Grundaufzeichnung oder in einem Grundbuch zu erfassen. Nach den GoB müssen 
die Geschäftsvorfälle grundsätzlich laufend gebucht werden (Journal). Es widerspricht 
dem Wesen der kaufmännischen Buchführung, sich zunächst auf die Sammlung von 
Belegen zu beschränken und nach Ablauf einer langen Zeit auf Grund dieser Belege die 
Geschäftsvorfälle in Grundaufzeichnungen oder Grundbüchern einzutragen (vgl. BFH-


--- Page 13 ---
 
Seite 13
Urteil vom 10. Juni 1954, BStBl III S. 298). Die Funktion der Grund(buch)aufzeich-
nungen kann auf Dauer auch durch eine geordnete und übersichtliche Belegablage 
erfüllt werden (§ 239 Absatz 4 HGB; § 146 Absatz 5 AO; H 5.2 „Grundbuchaufzeich-
nungen“ EStH). 
47 
Jede nicht durch die Verhältnisse des Betriebs oder des Geschäftsvorfalls zwingend 
bedingte Zeitspanne zwischen dem Eintritt des Vorganges und seiner laufenden 
Erfassung in Grund(buch)aufzeichnungen ist bedenklich. Eine Erfassung von unbaren 
Geschäftsvorfällen innerhalb von zehn Tagen ist unbedenklich. Wegen der Forderung 
nach zeitnaher chronologischer Erfassung der Geschäftsvorfälle ist zu verhindern, dass 
die Geschäftsvorfälle buchmäßig für längere Zeit in der Schwebe gehalten werden und 
sich hierdurch die Möglichkeit eröffnet, sie später anders darzustellen, als sie richtiger-
weise darzustellen gewesen wären, oder sie ganz außer Betracht zu lassen und im 
privaten, sich in der Buchführung nicht niederschlagenden Bereich abzuwickeln.  
Bei zeitlichen Abständen zwischen der Entstehung eines Geschäftsvorfalls und seiner 
Erfassung sind daher geeignete Maßnahmen zur Sicherung der Vollständigkeit zu 
treffen. 
48 
Kasseneinnahmen und Kassenausgaben sind nach § 146 Absatz 1 Satz 2 AO täglich 
festzuhalten. 
49 
Es ist nicht zu beanstanden, wenn Waren- und Kostenrechnungen, die innerhalb von 
acht Tagen nach Rechnungseingang oder innerhalb der ihrem gewöhnlichen Durchlauf 
durch den Betrieb entsprechenden Zeit beglichen werden, kontokorrentmäßig nicht 
(z. B. Geschäftsfreundebuch, Personenkonten) erfasst werden (vgl. R 5.2 Absatz 1 
EStR).  
50 
Werden bei der Erstellung der Bücher Geschäftsvorfälle nicht laufend, sondern nur 
periodenweise gebucht bzw. den Büchern vergleichbare Aufzeichnungen der Nicht-
buchführungspflichtigen nicht laufend, sondern nur periodenweise erstellt, dann ist 
dies unter folgenden Voraussetzungen nicht zu beanstanden: 
• Die Geschäftsvorfälle werden vorher zeitnah (bare Geschäftsvorfälle täglich, 
unbare Geschäftsvorfälle innerhalb von zehn Tagen) in Grund(buch)aufzeichnun-
gen oder Grundbüchern festgehalten und durch organisatorische Vorkehrungen ist 
sichergestellt, dass die Unterlagen bis zu ihrer Erfassung nicht verloren gehen, 
z. B. durch laufende Nummerierung der eingehenden und ausgehenden Rechnun-
gen, durch Ablage in besonderen Mappen und Ordnern oder durch elektronische 
Grund(buch)aufzeichnungen in Kassensystemen, Warenwirtschaftssystemen, 
Fakturierungssystemen etc., 
• die Vollständigkeit der Geschäftsvorfälle wird im Einzelfall gewährleistet und 


--- Page 14 ---
 
Seite 14
• es wurde zeitnah eine Zuordnung (Kontierung, mindestens aber die Zuordnung 
betrieblich / privat, Ordnungskriterium für die Ablage) vorgenommen. 
51 
Jeder Geschäftsvorfall ist periodengerecht der Abrechnungsperiode zuzuordnen, in der 
er angefallen ist. Zwingend ist die Zuordnung zum jeweiligen Geschäftsjahr oder zu 
einer nach Gesetz, Satzung oder Rechnungslegungszweck vorgeschriebenen kürzeren 
Rechnungsperiode. 
 
52 
Erfolgt die Belegsicherung oder die Erfassung von Geschäftsvorfällen unmittelbar 
nach Eingang oder Entstehung mittels DV-System (elektronische Grund(buch)auf-
zeichnungen), so stellt sich die Frage der Zumutbarkeit und Praktikabilität hinsichtlich 
der zeitgerechten Erfassung/Belegsicherung und längerer Fristen nicht. Erfüllen die 
Erfassungen Belegfunktion bzw. dienen sie der Belegsicherung (auch für Vorsysteme, 
wie Kasseneinzelaufzeichnungen und Warenwirtschaftssystem), dann ist eine unproto-
kollierte Änderung nicht mehr zulässig (siehe unter 3.2.5). Bei zeitlichen Abständen 
zwischen Erfassung und Buchung, die über den Ablauf des folgenden Monats hinaus-
gehen, sind die Ordnungsmäßigkeitsanforderungen nur dann erfüllt, wenn die 
Geschäftsvorfälle vorher fortlaufend richtig und vollständig in Grund(buch)aufzeich-
nungen oder Grundbüchern festgehalten werden (vgl. Rz. 50). Zur Erfüllung der Funk-
tion der Grund(buch)aufzeichnung vgl. Rz. 46. 
3.2.4 Ordnung (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB) 
53 
Der Grundsatz der Klarheit verlangt u. a. eine systematische Erfassung und übersicht-
liche, eindeutige und nachvollziehbare Buchungen.  
54 
Die geschäftlichen Unterlagen dürfen nicht planlos gesammelt und aufbewahrt wer-
den. Ansonsten würde dies mit zunehmender Zahl und Verschiedenartigkeit der 
Geschäftsvorfälle zur Unübersichtlichkeit der Buchführung führen, einen jederzeitigen 
Abschluss unangemessen erschweren und die Gefahr erhöhen, dass Unterlagen ver-
lorengehen oder später leicht aus dem Buchführungswerk entfernt werden können. 
Hieraus folgt, dass die Bücher und Aufzeichnungen nach bestimmten Ordnungsprin-
zipien geführt werden müssen und eine Sammlung und Aufbewahrung der Belege not-
wendig ist, durch die im Rahmen des Möglichen gewährleistet wird, dass die 
Geschäftsvorfälle leicht und identifizierbar feststellbar und für einen die Lage des 
Vermögens darstellenden Abschluss unverlierbar sind (BFH-Urteil vom 26. März 
1968, BStBl II S. 527).  
55 
In der Regel verstößt die nicht getrennte Verbuchung von baren und unbaren 
Geschäftsvorfällen oder von nicht steuerbaren, steuerfreien und steuerpflichtigen 
Umsätzen ohne genügende Kennzeichnung gegen die Grundsätze der Wahrheit und 
Klarheit einer kaufmännischen Buchführung. Die nicht getrennte Aufzeichnung von 
nicht steuerbaren, steuerfreien und steuerpflichtigen Umsätzen ohne genügende 


--- Page 15 ---
 
Seite 15
Kennzeichnung verstößt in der Regel gegen steuerrechtliche Anforderungen (z. B. 
§ 22 UStG). Eine kurzzeitige gemeinsame Erfassung von baren und unbaren Tages-
geschäften im Kassenbuch ist regelmäßig nicht zu beanstanden, wenn die ursprünglich 
im Kassenbuch erfassten unbaren Tagesumsätze (z. B. EC-Kartenumsätze) gesondert 
kenntlich gemacht sind und nachvollziehbar unmittelbar nachfolgend wieder aus dem 
Kassenbuch auf ein gesondertes Konto aus- bzw. umgetragen werden, soweit die 
Kassensturzfähigkeit der Kasse weiterhin gegeben ist. 
56 
Bei der doppelten Buchführung sind die Geschäftsvorfälle so zu verarbeiten, dass sie 
geordnet darstellbar sind und innerhalb angemessener Zeit ein Überblick über die Ver-
mögens- und Ertragslage gewährleistet ist.  
57 
Die Buchungen müssen einzeln und sachlich geordnet nach Konten dargestellt (Kon-
tenfunktion) und unverzüglich lesbar gemacht werden können. Damit bei Bedarf für 
einen zurückliegenden Zeitpunkt ein Zwischenstatus oder eine Bilanz mit Gewinn- 
und Verlustrechnung aufgestellt werden kann, sind die Konten nach Abschluss-
positionen zu sammeln und nach Kontensummen oder Salden fortzuschreiben 
(Hauptbuch, siehe unter 5.4). 
3.2.5 Unveränderbarkeit (§ 146 Absatz 4 AO, § 239 Absatz 3 HGB) 
58 
Eine Buchung oder eine Aufzeichnung darf nicht in einer Weise verändert werden, 
dass der ursprüngliche Inhalt nicht mehr feststellbar ist. Auch solche Veränderungen 
dürfen nicht vorgenommen werden, deren Beschaffenheit es ungewiss lässt, ob sie 
ursprünglich oder erst später gemacht worden sind (§ 146 Absatz 4 AO, § 239 
Absatz 3 HGB). 
  
59 
Veränderungen und Löschungen von und an elektronischen Buchungen oder Auf-
zeichnungen (vgl. Rzn. 3 bis 5) müssen daher so protokolliert werden, dass die 
Voraussetzungen des § 146 Absatz 4 AO bzw. § 239 Absatz 3 HGB erfüllt sind (siehe 
auch unter 8). Für elektronische Dokumente und andere elektronische Unterlagen, die 
gem. § 147 AO aufbewahrungspflichtig und nicht Buchungen oder Aufzeichnungen 
sind, gilt dies sinngemäß.  
Beispiel 4: 
Der Steuerpflichtige erstellt über ein Fakturierungssystem Ausgangsrechnungen und 
bewahrt die inhaltlichen Informationen elektronisch auf (zum Beispiel in seinem 
Fakturierungssystem). Die Lesbarmachung der abgesandten Handels- und Geschäfts-
briefe aus dem Fakturierungssystem erfolgt jeweils unter Berücksichtigung der in den 
aktuellen Stamm- und Bewegungsdaten enthaltenen Informationen. 


--- Page 16 ---
 
Seite 16
In den Stammdaten ist im Jahr 01 der Steuersatz 16 % und der Firmenname des Kun-
den A hinterlegt. Durch Umfirmierung des Kunden A zu B und Änderung des Steuer-
satzes auf 19 % werden die Stammdaten im Jahr 02 geändert. Eine Historisierung der 
Stammdaten erfolgt nicht. 
Der Steuerpflichtige ist im Jahr 02 nicht mehr in der Lage, die inhaltliche Überein-
stimmung der abgesandten Handels- und Geschäftsbriefe mit den ursprünglichen 
Inhalten bei Lesbarmachung sicher zu stellen. 
 
60 
Der Nachweis der Durchführung der in dem jeweiligen Verfahren vorgesehenen 
Kontrollen ist u. a. durch Verarbeitungsprotokolle sowie durch die Verfahrens-
dokumentation (siehe unter 6. und unter 10.1) zu erbringen. 
4. Belegwesen (Belegfunktion) 
61 
Jeder Geschäftsvorfall ist urschriftlich bzw. als Kopie der Urschrift zu belegen.  
Ist kein Fremdbeleg vorhanden, muss ein Eigenbeleg erstellt werden. Zweck der 
Belege ist es, den sicheren und klaren Nachweis über den Zusammenhang zwischen 
den Vorgängen in der Realität einerseits und dem aufgezeichneten oder gebuchten 
Inhalt in Büchern oder sonst erforderlichen Aufzeichnungen und ihre Berechtigung 
andererseits zu erbringen (Belegfunktion). Auf die Bezeichnung als „Beleg“ kommt es 
nicht an. Die Belegfunktion ist die Grundvoraussetzung für die Beweiskraft der 
Buchführung und sonst erforderlicher Aufzeichnungen. Sie gilt auch bei Einsatz eines 
DV-Systems.  
62 
Inhalt und Umfang der in den Belegen enthaltenen Informationen sind insbesondere 
von der Belegart (z. B. Aufträge, Auftragsbestätigungen, Bescheide über Steuern oder 
Gebühren, betriebliche Kontoauszüge, Gutschriften, Lieferscheine, Lohn- und 
Gehaltsabrechnungen, Barquittungen, Rechnungen, Verträge, Zahlungsbelege) und der 
eingesetzten Verfahren abhängig.  
 
63 
Empfangene oder abgesandte Handels- oder Geschäftsbriefe erhalten erst mit dem 
Kontierungsvermerk und der Verbuchung auch die Funktion eines Buchungsbelegs. 
64 
Zur Erfüllung der Belegfunktionen sind deshalb Angaben zur Kontierung, zum 
Ordnungskriterium für die Ablage und zum Buchungsdatum auf dem Papierbeleg 
erforderlich. Bei einem elektronischen Beleg kann dies auch durch die Verbindung mit 
einem Datensatz mit Angaben zur Kontierung oder durch eine elektronische Verknüp-
fung (z. B. eindeutiger Index, Barcode) erfolgen. Ein Steuerpflichtiger hat andernfalls 
durch organisatorische Maßnahmen sicherzustellen, dass die Geschäftsvorfälle auch 
ohne Angaben auf den Belegen in angemessener Zeit progressiv und retrograd nach-
prüfbar sind.  


--- Page 17 ---
 
Seite 17
 
Korrektur- bzw. Stornobuchungen müssen auf die ursprüngliche Buchung rück-
beziehbar sein. 
65 
Ein Buchungsbeleg in Papierform oder in elektronischer Form (z. B. Rechnung) kann 
einen oder mehrere Geschäftsvorfälle enthalten.  
66 
Aus der Verfahrensdokumentation (siehe unter 10.1) muss ersichtlich sein, wie die 
elektronischen Belege erfasst, empfangen, verarbeitet, ausgegeben und aufbewahrt 
(zur Aufbewahrung siehe unter 9.) werden. 
4.1 Belegsicherung 
67 
Die Belege in Papierform oder in elektronischer Form sind zeitnah, d. h. möglichst 
unmittelbar nach Eingang oder Entstehung gegen Verlust zu sichern (vgl. zur zeit-
gerechten Belegsicherung unter 3.2.3, vgl. zur Aufbewahrung unter 9.).  
68 
Bei Papierbelegen erfolgt eine Sicherung z. B. durch laufende Nummerierung der ein-
gehenden und ausgehenden Lieferscheine und Rechnungen, durch laufende Ablage in 
besonderen Mappen und Ordnern, durch zeitgerechte Erfassung in Grund(buch)auf-
zeichnungen oder durch laufende Vergabe eines Barcodes und anschließende bildliche 
Erfassung der Papierbelege im Sinne des § 147 Absatz 2 AO (siehe Rz. 130). 
69 
Bei elektronischen Belegen (z. B. Abrechnung aus Fakturierung) kann die laufende 
Nummerierung automatisch vergeben werden (z. B. durch eine eindeutige Beleg-
nummer).  
70 
Die Belegsicherung kann organisatorisch und technisch mit der Zuordnung zwischen 
Beleg und Grund(buch)aufzeichnung oder Buchung verbunden werden. 
4.2 Zuordnung zwischen Beleg und Grund(buch)aufzeichnung oder Buchung 
71 
Die Zuordnung zwischen dem einzelnen Beleg und der dazugehörigen Grund(buch)auf-
zeichnung oder Buchung kann anhand von eindeutigen Zuordnungsmerkmalen (z. B. 
Index, Paginiernummer, Dokumenten-ID) und zusätzlichen Identifikationsmerkmalen 
für die Papierablage oder für die Such- und Filtermöglichkeit bei elektronischer Beleg-
ablage gewährleistet werden. Gehören zu einer Grund(buch)aufzeichnung oder Buchung 
mehrere Belege (z. B. Rechnung verweist für Menge und Art der gelieferten Gegenstän-
de nur auf Lieferschein), bedarf es zusätzlicher Zuordnungs- und Identifikationsmerk-
male für die Verknüpfung zwischen den Belegen und der Grund(buch)aufzeichnung 
oder Buchung. 
72 
Diese Zuordnungs- und Identifizierungsmerkmale aus dem Beleg müssen bei der Auf-
zeichnung oder Verbuchung in die Bücher oder Aufzeichnungen übernommen werden, 
um eine progressive und retrograde Prüfbarkeit zu ermöglichen. 


--- Page 18 ---
 
Seite 18
73 
Die Ablage der Belege und die Zuordnung zwischen Beleg und Aufzeichnung müssen 
in angemessener Zeit nachprüfbar sein. So kann z. B. Beleg- oder Buchungsdatum, 
Kontoauszugnummer oder Name bei umfangreichem Beleganfall mangels Eindeutig-
keit in der Regel kein geeignetes Zuordnungsmerkmal für den einzelnen Geschäftsvor-
fall sein. 
74 
Beispiel 5: 
Ein Steuerpflichtiger mit ausschließlich unbaren Geschäftsvorfällen erhält nach 
Abschluss eines jeden Monats von seinem Kreditinstitut einen Kontoauszug in 
Papierform mit vielen einzelnen Kontoblättern. Für die Zuordnung der Belege und 
Aufzeichnungen erfasst der Unternehmer ausschließlich die Kontoauszugsnummer. 
Allein anhand der Kontoauszugsnummer - ohne zusätzliche Angabe der Blattnummer 
und der Positionsnummer - ist eine Zuordnung von Beleg und Aufzeichnung oder 
Buchung in angemessener Zeit nicht nachprüfbar. 
4.3 Erfassungsgerechte Aufbereitung der Buchungsbelege 
75 
Eine erfassungsgerechte Aufbereitung der Buchungsbelege in Papierform oder die 
entsprechende Übernahme von Beleginformationen aus elektronischen Belegen 
(Daten, Datensätze, elektronische Dokumente und elektronische Unterlagen) ist 
sicherzustellen. Diese Aufbereitung der Belege ist insbesondere bei Fremdbelegen von 
Bedeutung, da der Steuerpflichtige im Allgemeinen keinen Einfluss auf die Gestaltung 
der ihm zugesandten Handels- und Geschäftsbriefe (z. B. Eingangsrechnungen) hat. 
 
76 
Werden neben bildhaften Urschriften auch elektronische Meldungen bzw. Datensätze 
ausgestellt (identische Mehrstücke derselben Belegart), ist die Aufbewahrung der 
tatsächlich weiterverarbeiteten Formate (buchungsbegründende Belege) ausreichend, 
sofern diese über die höchste maschinelle Auswertbarkeit verfügen. In diesem Fall 
erfüllt das Format mit der höchsten maschinellen Auswertbarkeit mit dessen 
vollständigem Dateninhalt die Belegfunktion und muss mit dessen vollständigem 
Inhalt gespeichert werden. Andernfalls sind beide Formate aufzubewahren. Dies gilt 
entsprechend, wenn mehrere elektronische Meldungen bzw. mehrere Datensätze ohne 
bildhafte Urschrift ausgestellt werden. Dies gilt auch für elektronische Meldungen 
(strukturierte Daten, wie z. B. ein monatlicher Kontoauszug im CSV-Format oder als 
XML-File), für die inhaltsgleiche bildhafte Dokumente zusätzlich bereitgestellt 
werden. Eine zusätzliche Archivierung der inhaltsgleichen Kontoauszüge in PDF oder 
Papier kann bei Erfüllung der Belegfunktion durch die strukturierten 
Kontoumsatzdaten entfallen.  
Bei Einsatz eines Fakturierungsprogramms muss unter Berücksichtigung der vorge-
nannten Voraussetzungen keine bildhafte Kopie der Ausgangsrechnung (z. B. in Form 


--- Page 19 ---
 
Seite 19
einer PDF-Datei) ab Erstellung gespeichert bzw. aufbewahrt werden, wenn jederzeit 
auf Anforderung ein entsprechendes Doppel der Ausgangsrechnung erstellt werden 
kann. 
Hierfür sind u. a. folgende Voraussetzungen zu beachten: 
• Entsprechende Stammdaten (z. B. Debitoren, Warenwirtschaft etc.) werden 
laufend historisiert 
• AGB werden ebenfalls historisiert und aus der Verfahrensdokumentation ist 
ersichtlich, welche AGB bei Erstellung der Originalrechnung verwendet 
wurden 
• Originallayout des verwendeten Geschäftsbogens wird als Muster (Layer) 
gespeichert und bei Änderungen historisiert. Zudem ist aus der Verfahrens-
dokumentation ersichtlich, welches Format bei Erstellung der Originalrech-
nung verwendet wurde (idealerweise kann bei Ausdruck oder Lesbarmachung 
des Rechnungsdoppels dieses Originallayout verwendet werden). 
• Weiterhin sind die Daten des Fakturierungsprogramms in maschinell auswert-
barer Form und unveränderbar aufzubewahren. 
77 
Jedem Geschäftsvorfall muss ein Beleg zugrunde liegen, mit folgenden Inhalten: 
 
Bezeichnung 
Begründung 
Eindeutige Belegnummer (z. B. Index, 
Paginiernummer, Dokumenten-ID, 
fortlaufende 
Rechnungsausgangsnummer) 
  
Angabe zwingend (§ 146 Absatz 1 Satz 1 
AO, einzeln, vollständig, geordnet) 
Kriterium für Vollständigkeitskontrolle 
(Belegsicherung) 
Bei umfangreichem Beleganfall ist Zuord-
nung und Identifizierung regelmäßig nicht 
aus Belegdatum oder anderen Merkmalen 
eindeutig ableitbar. 
Sofern die Fremdbelegnummer eine ein-
deutige Zuordnung zulässt, kann auch 
diese verwendet werden. 
Belegaussteller und -empfänger 
Soweit dies zu den branchenüblichen Min-
destaufzeichnungspflichten gehört und 
keine Aufzeichnungserleichterungen 
bestehen (z. B. § 33 UStDV) 
Betrag bzw. Mengen- oder Wertanga-
ben, aus denen sich der zu buchende 
Angabe zwingend (BFH vom 12. Mai 
1966, BStBl III S. 371); Dokumentation 


--- Page 20 ---
 
Seite 20
Bezeichnung 
Begründung 
Betrag ergibt 
einer Veränderung des Anlage- und 
Umlaufvermögens sowie des Eigen- und 
Fremdkapitals 
Währungsangabe und Wechselkurs bei 
Fremdwährung 
Ermittlung des Buchungsbetrags 
Hinreichende Erläuterung des 
Geschäftsvorfalls 
Hinweis auf BFH-Urteil vom 12. Mai 
1966, BStBl III S. 371; BFH-Urteil vom 
1. Oktober 1969, BStBl II 1970 S. 45 
 
Belegdatum 
 
Angabe zwingend (§ 146 Absatz 1 Satz 1 
AO, zeitgerecht). 
Identifikationsmerkmale für eine 
chronologische Erfassung, bei 
Bargeschäften regelmäßig Zeitpunkt des 
Geschäftsvorfalls 
Evtl. zusätzliche Erfassung der Belegzeit 
bei umfangreichem Beleganfall 
erforderlich 
Verantwortlicher Aussteller, soweit 
vorhanden 
Z. B. Bediener der Kasse 
 
Vgl. Rz. 85 zu den Inhalten der Grund(buch)aufzeichnungen. 
Vgl. Rz. 94 zu den Inhalten des Journals.  
78 
Für umsatzsteuerrechtliche Zwecke können weitere Angaben erforderlich sein.  
Dazu gehören beispielsweise die Rechnungsangaben nach §§ 14, 14a UStG und § 33 
UStDV. 
79 
Buchungsbelege sowie abgesandte oder empfangene Handels- oder Geschäftsbriefe in 
Papierform oder in elektronischer Form enthalten darüber hinaus vielfach noch weitere 
Informationen, die zum Verständnis und zur Überprüfung der für die Besteuerung 
gesetzlich vorgeschriebenen Aufzeichnungen im Einzelfall von Bedeutung und damit 
ebenfalls aufzubewahren sind. Dazu gehören z. B.: 
• Mengen- oder Wertangaben zur Erläuterung des Buchungsbetrags, sofern nicht 
bereits unter Rz. 77 berücksichtigt, 


--- Page 21 ---
 
Seite 21
• Einzelpreis (z. B. zur Bewertung), 
• Valuta, Fälligkeit (z. B. zur Bewertung), 
• Angaben zu Skonti, Rabatten (z. B. zur Bewertung), 
• Zahlungsart (bar, unbar), 
• Angaben zu einer Steuerbefreiung.  
4.4 Besonderheiten 
80 
Bei DV-gestützten Prozessen wird der Nachweis der zutreffenden Abbildung von 
Geschäftsvorfällen oft nicht durch konventionelle Belege erbracht (z. B. Buchungen 
aus Fakturierungssätzen, die durch Multiplikation von Preisen mit entnommenen Men-
gen aus der Betriebsdatenerfassung gebildet werden). Die Erfüllung der Belegfunktion 
ist dabei durch die ordnungsgemäße Anwendung des jeweiligen Verfahrens wie folgt 
nachzuweisen: 
• Dokumentation der programminternen Vorschriften zur Generierung der 
Buchungen, 
• Nachweis oder Bestätigung, dass die in der Dokumentation enthaltenen Vorschrif-
ten einem autorisierten Änderungsverfahren unterlegen haben (u. a. Zugriffs-
schutz, Versionsführung, Test- und Freigabeverfahren), 
• Nachweis der Anwendung des genehmigten Verfahrens sowie 
• Nachweis der tatsächlichen Durchführung der einzelnen Buchungen. 
 
81 
Bei Dauersachverhalten sind die Ursprungsbelege Basis für die folgenden Automatik-
buchungen. Bei (monatlichen) AfA-Buchungen nach Anschaffung eines abnutzbaren 
Wirtschaftsguts ist der Anschaffungsbeleg mit der AfA-Bemessungsgrundlage und 
weiteren Parametern (z. B. Nutzungsdauer) aufbewahrungspflichtig. Aus der Verfah-
rensdokumentation und der ordnungsmäßigen Anwendung des Verfahrens muss der 
automatische Buchungsvorgang nachvollziehbar sein. 
5. Aufzeichnung der Geschäftsvorfälle in zeitlicher Reihenfolge und in sach-
licher Ordnung (Grund(buch)aufzeichnungen, Journal- und 
Kontenfunktion) 
82 
Der Steuerpflichtige hat organisatorisch und technisch sicherzustellen, dass die elek-
tronischen Buchungen und sonst erforderlichen elektronischen Aufzeichnungen 
einzeln, vollständig, richtig, zeitgerecht und geordnet vorgenommen werden (§ 146 
Absatz 1 Satz 1 AO, § 239 Absatz 2 HGB). Jede Buchung oder Aufzeichnung muss im 
Zusammenhang mit einem Beleg stehen (BFH-Urteil vom 24. Juni 1997, BStBl II 
1998 S. 51).  


--- Page 22 ---
 
Seite 22
83 
Bei der doppelten Buchführung müssen alle Geschäftsvorfälle in zeitlicher Reihen-
folge (Grund(buch)aufzeichnung, Journalfunktion) und in sachlicher Gliederung 
(Hauptbuch, Kontenfunktion, siehe unter 5.4) darstellbar sein. Im Hauptbuch bzw. bei 
der Kontenfunktion verursacht jeder Geschäftsvorfall eine Buchung auf mindestens 
zwei Konten (Soll- und Habenbuchung).  
84 
Die Erfassung der Geschäftsvorfälle in elektronischen Grund(buch)aufzeichnungen 
(siehe unter 5.1 und 5.2) und die Verbuchung im Journal (siehe unter 5.3) kann organi-
satorisch und zeitlich auseinanderfallen (z. B. Grund(buch)aufzeichnung in Form von 
Kassenauftragszeilen). Erfüllen die Erfassungen Belegfunktion bzw. dienen sie der 
Belegsicherung, dann ist eine unprotokollierte Änderung nicht mehr zulässig (vgl. 
Rzn. 58 und 59). In diesen Fällen gelten die Ordnungsvorschriften bereits mit der 
ersten Erfassung der Geschäftsvorfälle und der Daten und müssen über alle nachfol-
genden Prozesse erhalten bleiben (z. B. Übergabe von Daten aus Vor- in Haupt-
systeme). 
5.1 Erfassung in Grund(buch)aufzeichnungen 
85 
Die fortlaufende Aufzeichnung der Geschäftsvorfälle erfolgt zunächst in Papierform 
oder in elektronischen Grund(buch)aufzeichnungen (Grundaufzeichnungsfunktion), 
um die Belegsicherung und die Garantie der Unverlierbarkeit des Geschäftsvorfalls zu 
gewährleisten. Sämtliche Geschäftsvorfälle müssen der zeitlichen Reihenfolge nach 
und materiell mit ihrem richtigen und erkennbaren Inhalt festgehalten werden. 
 
Zu den aufzeichnungspflichtigen Inhalten gehören 
• die in Rzn. 77, 78 und 79 enthaltenen Informationen, 
• das Erfassungsdatum, soweit abweichend vom Buchungsdatum 
Begründung: 
o Angabe zwingend (§ 146 Absatz 1 Satz 1 AO, zeitgerecht), 
o Zeitpunkt der Buchungserfassung und -verarbeitung, 
o Angabe der „Festschreibung“ (Veränderbarkeit nur mit Protokollie-
rung) zwingend, soweit nicht Unveränderbarkeit automatisch mit Erfas-
sung und Verarbeitung in Grund(buch)aufzeichnung. 
 
Vgl. Rz. 94 zu den Inhalten des Journals.  
86 
Die Grund(buch)aufzeichnungen sind nicht an ein bestimmtes System gebunden.  
Jedes System, durch das die einzelnen Geschäftsvorfälle fortlaufend, vollständig und 
richtig festgehalten werden, so dass die Grundaufzeichnungsfunktion erfüllt wird, ist 
ordnungsmäßig (vgl. BFH-Urteil vom 26. März 1968, BStBl II S. 527 für Buchfüh-
rungspflichtige).  


--- Page 23 ---
 
Seite 23
5.2 Digitale Grund(buch)aufzeichnungen 
87 
Sowohl beim Einsatz von Haupt- als auch von Vor- oder Nebensystemen ist eine 
Verbuchung im Journal des Hauptsystems (z. B. Finanzbuchhaltung) bis zum Ablauf 
des folgenden Monats nicht zu beanstanden, wenn die einzelnen Geschäftsvorfälle 
bereits in einem Vor- oder Nebensystem die Grundaufzeichnungsfunktion erfüllen und 
die Einzeldaten aufbewahrt werden.  
88 
Durch Erfassungs-, Übertragungs- und Verarbeitungskontrollen ist sicherzustellen, 
dass alle Geschäftsvorfälle vollständig erfasst oder übermittelt werden und danach 
nicht unbefugt (d. h. nicht ohne Zugriffsschutzverfahren) und nicht ohne Nachweis des 
vorausgegangenen Zustandes verändert werden können. Die Durchführung der Kon-
trollen ist zu protokollieren. Die konkrete Ausgestaltung der Protokollierung ist 
abhängig von der Komplexität und Diversifikation der Geschäftstätigkeit und der 
Organisationsstruktur sowie des eingesetzten DV-Systems.  
89 
Neben den Daten zum Geschäftsvorfall selbst müssen auch alle für die Verarbeitung 
erforderlichen Tabellendaten (Stammdaten, Bewegungsdaten, Metadaten wie z. B. 
Grund- oder Systemeinstellungen, geänderte Parameter), deren Historisierung und 
Programme gespeichert sein. Dazu gehören auch Informationen zu Kriterien, die eine 
Abgrenzung zwischen den steuerrechtlichen, den handelsrechtlichen und anderen 
Buchungen (z. B. nachrichtliche Datensätze zu Fremdwährungen, alternative Bewer-
tungsmethoden, statistische Buchungen, GuV-Kontennullstellungen, Summenkonten) 
ermöglichen. 
5.3 Verbuchung im Journal (Journalfunktion) 
90 
Die Journalfunktion erfordert eine vollständige, zeitgerechte und formal richtige Erfas-
sung, Verarbeitung und Wiedergabe der eingegebenen Geschäftsvorfälle. Sie dient 
dem Nachweis der tatsächlichen und zeitgerechten Verarbeitung der Geschäftsvorfälle. 
91 
Werden die unter 5.1 genannten Voraussetzungen bereits mit fortlaufender Verbu-
chung im Journal erfüllt, ist eine zusätzliche Erfassung in Grund(buch)aufzeichnungen 
nicht erforderlich. Eine laufende Aufzeichnung unmittelbar im Journal genügt den 
Erfordernissen der zeitgerechten Erfassung in Grund(buch)aufzeichnungen (vgl. BFH-
Urteil vom 16. September 1964, BStBl III S. 654). Zeitversetzte Buchungen im 
Journal genügen nur dann, wenn die Geschäftsvorfälle vorher fortlaufend richtig und 
vollständig in Grundaufzeichnungen oder Grundbüchern aufgezeichnet werden.  
Die Funktion der Grund(buch)aufzeichnungen kann auf Dauer auch durch eine geord-
nete und übersichtliche Belegablage erfüllt werden (§ 239 Absatz 4 HGB, § 146 
Absatz 5 AO, H 5.2 „Grundbuchaufzeichnungen“ EStH; vgl. Rz. 46).  


--- Page 24 ---
 
Seite 24
92 
Die Journalfunktion ist nur erfüllt, wenn die gespeicherten Aufzeichnungen gegen 
Veränderung oder Löschung geschützt sind.  
93 
Fehlerhafte Buchungen können wirksam und nachvollziehbar durch Stornierungen 
oder Neubuchungen geändert werden (siehe unter 8.). Es besteht deshalb weder ein 
Bedarf noch die Notwendigkeit für weitere nachträgliche Veränderungen einer einmal 
erfolgten Buchung. Bei der doppelten Buchführung kann die Journalfunktion 
zusammen mit der Kontenfunktion erfüllt werden, indem bereits bei der erstmaligen 
Erfassung des Geschäftsvorfalls alle für die sachliche Zuordnung notwendigen 
Informationen erfasst werden. 
 
94 
Zur Erfüllung der Journalfunktion und zur Ermöglichung der Kontenfunktion sind bei 
der Buchung insbesondere die nachfolgenden Angaben zu erfassen oder bereit zu 
stellen: 
• Eindeutige Belegnummer (siehe Rz. 77), 
• Buchungsbetrag (siehe Rz. 77), 
• Währungsangabe und Wechselkurs bei Fremdwährung (siehe Rz. 77), 
• Hinreichende Erläuterung des Geschäftsvorfalls (siehe Rz. 77) - kann (bei 
Erfüllung der Journal- und Kontenfunktion) im Einzelfall bereits durch andere in 
Rz. 94 aufgeführte Angaben gegeben sein, 
• Belegdatum, soweit nicht aus den Grundaufzeichnungen ersichtlich (siehe Rzn. 77 
und 85) 
• Buchungsdatum, 
• Erfassungsdatum, soweit nicht aus der Grundaufzeichnung ersichtlich (siehe 
Rz. 85), 
• Autorisierung soweit vorhanden, 
• Buchungsperiode/Voranmeldungszeitraum (Ertragsteuer/Umsatzsteuer), 
• Umsatzsteuersatz (siehe Rz. 78), 
• Steuerschlüssel, soweit vorhanden (siehe Rz. 78), 
• Umsatzsteuerbetrag (siehe Rz. 78), 
• Umsatzsteuerkonto (siehe Rz. 78), 
• Umsatzsteuer-Identifikationsnummer (siehe Rz. 78), 
• Steuernummer (siehe Rz. 78), 
• Konto und Gegenkonto, 
• Buchungsschlüssel (soweit vorhanden), 
• Soll- und Haben-Betrag, 
• eindeutige Identifikationsnummer (Schlüsselfeld) des Geschäftsvorfalls (soweit 
Aufteilung der Geschäftsvorfälle in Teilbuchungssätze [Buchungs-Halbsätze] oder 
zahlreiche Soll- oder Habenkonten [Splitbuchungen] vorhanden). Über die einheit-


--- Page 25 ---
 
Seite 25
liche und je Wirtschaftsjahr eindeutige Identifikationsnummer des Geschäftsvor-
falls muss die Identifizierung und Zuordnung aller Teilbuchungen einschließlich 
Steuer-, Sammel-, Verrechnungs- und Interimskontenbuchungen eines Geschäfts-
vorfalls gewährleistet sein.  
5.4 Aufzeichnung der Geschäftsvorfälle in sachlicher Ordnung (Hauptbuch) 
95 
Die Geschäftsvorfälle sind so zu verarbeiten, dass sie geordnet darstellbar sind 
(Kontenfunktion) und damit die Grundlage für einen Überblick über die Vermögens- 
und Ertragslage darstellen. Zur Erfüllung der Kontenfunktion bei Bilanzierenden 
müssen Geschäftsvorfälle nach Sach- und Personenkonten geordnet dargestellt 
werden.  
96 
Die Kontenfunktion verlangt, dass die im Journal in zeitlicher Reihenfolge einzeln 
aufgezeichneten Geschäftsvorfälle auch in sachlicher Ordnung auf Konten dargestellt 
werden. Damit bei Bedarf für einen zurückliegenden Zeitpunkt ein Zwischenstatus 
oder eine Bilanz mit Gewinn- und Verlustrechnung aufgestellt werden kann, müssen 
Eröffnungsbilanzbuchungen und alle Abschlussbuchungen in den Konten enthalten 
sein. Die Konten sind nach Abschlussposition zu sammeln und nach Kontensummen 
oder Salden fortzuschreiben.  
97 
Werden innerhalb verschiedener Bereiche des DV-Systems oder zwischen unter-
schiedlichen DV-Systemen differierende Ordnungskriterien verwendet, so müssen 
entsprechende Zuordnungstabellen (z. B. elektronische Mappingtabellen) vorgehalten 
werden (z. B. Wechsel des Kontenrahmens, unterschiedliche Nummernkreise in Vor- 
und Hauptsystem). Dies gilt auch bei einer elektronischen Übermittlung von Daten an 
die Finanzbehörde (z. B. unterschiedliche Ordnungskriterien in Bilanz/GuV und EÜR 
einerseits und USt-Voranmeldung, LSt-Anmeldung, Anlage EÜR und E-Bilanz ande-
rerseits). Sollte die Zuordnung mit elektronischen Verlinkungen oder Schlüsselfeldern 
erfolgen, sind die Verlinkungen in dieser Form vorzuhalten.  
98 
Die vorstehenden Ausführungen gelten für die Nebenbücher entsprechend. 
99 
Bei der Übernahme verdichteter Zahlen ins Hauptsystem müssen die zugehörigen Ein-
zelaufzeichnungen aus den Vor- und Nebensystemen erhalten bleiben. 
6. Internes Kontrollsystem (IKS) 
100 
Für die Einhaltung der Ordnungsvorschriften des § 146 AO (siehe unter 3.) hat der 
Steuerpflichtige Kontrollen einzurichten, auszuüben und zu protokollieren.  
Hierzu gehören beispielsweise 
• Zugangs- und Zugriffsberechtigungskontrollen auf Basis entsprechender Zugangs- 
und Zugriffsberechtigungskonzepte (z. B. spezifische Zugangs- und 


--- Page 26 ---
 
Seite 26
Zugriffsberechtigungen), 
• Funktionstrennungen, 
• Erfassungskontrollen (Fehlerhinweise, Plausibilitätsprüfungen), 
• Abstimmungskontrollen bei der Dateneingabe, 
• Verarbeitungskontrollen, 
• Schutzmaßnahmen gegen die beabsichtigte und unbeabsichtigte Verfälschung von 
Programmen, Daten und Dokumenten. 
Die konkrete Ausgestaltung des Kontrollsystems ist abhängig von der Komplexität 
und Diversifikation der Geschäftstätigkeit und der Organisationsstruktur sowie des 
eingesetzten DV-Systems. 
101 
Im Rahmen eines funktionsfähigen IKS muss auch anlassbezogen (z. B. System-
wechsel) geprüft werden, ob das eingesetzte DV-System tatsächlich dem dokumen-
tierten System entspricht (siehe Rz. 155 zu den Rechtsfolgen bei fehlender oder unge-
nügender Verfahrensdokumentation). 
102 
Die Beschreibung des IKS ist Bestandteil der Verfahrensdokumentation (siehe 
unter 10.1). 
7. Datensicherheit 
103 
Der Steuerpflichtige hat sein DV-System gegen Verlust (z. B. Unauffindbarkeit, Ver-
nichtung, Untergang und Diebstahl) zu sichern und gegen unberechtigte Eingaben und 
Veränderungen (z. B. durch Zugangs- und Zugriffskontrollen) zu schützen. 
104 
Werden die Daten, Datensätze, elektronischen Dokumente und elektronischen Unter-
lagen nicht ausreichend geschützt und können deswegen nicht mehr vorgelegt werden, 
so ist die Buchführung formell nicht mehr ordnungsmäßig. 
105 
Beispiel 6: 
Unternehmer überschreibt unwiderruflich die Finanzbuchhaltungsdaten des Vorjahres 
mit den Daten des laufenden Jahres. 
Die sich daraus ergebenden Rechtsfolgen sind vom jeweiligen Einzelfall abhängig. 
106 
Die Beschreibung der Vorgehensweise zur Datensicherung ist Bestandteil der Verfah-
rensdokumentation (siehe unter 10.1). Die konkrete Ausgestaltung der Beschreibung 
ist abhängig von der Komplexität und Diversifikation der Geschäftstätigkeit und der 
Organisationsstruktur sowie des eingesetzten DV-Systems. 


--- Page 27 ---
 
Seite 27
8. Unveränderbarkeit, Protokollierung von Änderungen 
107 
Nach § 146 Absatz 4 AO darf eine Buchung oder Aufzeichnung nicht in einer Weise 
verändert werden, dass der ursprüngliche Inhalt nicht mehr feststellbar ist. Auch sol-
che Veränderungen dürfen nicht vorgenommen werden, deren Beschaffenheit es unge-
wiss lässt, ob sie ursprünglich oder erst später gemacht worden sind. 
108 
Das zum Einsatz kommende DV-Verfahren muss die Gewähr dafür bieten, dass alle 
Informationen (Programme und Datenbestände), die einmal in den Verarbeitungs-
prozess eingeführt werden (Beleg, Grundaufzeichnung, Buchung), nicht mehr unter-
drückt oder ohne Kenntlichmachung überschrieben, gelöscht, geändert oder verfälscht 
werden können. Bereits in den Verarbeitungsprozess eingeführte Informationen 
(Beleg, Grundaufzeichnung, Buchung) dürfen nicht ohne Kenntlichmachung durch 
neue Daten ersetzt werden. 
109 
Beispiele 7 für unzulässige Vorgänge: 
• Elektronische Grund(buch)aufzeichnungen aus einem Kassen- oder Warenwirt-
schaftssystem werden über eine Datenschnittstelle in ein Officeprogramm expor-
tiert, dort unprotokolliert editiert und anschließend über eine Datenschnittstelle 
reimportiert. 
• Vorerfassungen und Stapelbuchungen werden bis zur Erstellung des Jahresab-
schlusses und darüber hinaus offen gehalten. Alle Eingaben können daher 
unprotokolliert geändert werden. 
 
110 
Die Unveränderbarkeit der Daten, Datensätze, elektronischen Dokumente und elektro-
nischen Unterlagen (vgl. Rzn. 3 bis 5) kann sowohl hardwaremäßig (z. B. unveränder-
bare und fälschungssichere Datenträger) als auch softwaremäßig (z. B. Sicherungen, 
Sperren, Festschreibung, Löschmerker, automatische Protokollierung, Historisierun-
gen, Versionierungen) als auch organisatorisch (z. B. mittels Zugriffsberechtigungs-
konzepten) gewährleistet werden. Die Ablage von Daten und elektronischen Doku-
menten in einem Dateisystem erfüllt die Anforderungen der Unveränderbarkeit 
regelmäßig nicht, soweit nicht zusätzliche Maßnahmen ergriffen werden, die eine 
Unveränderbarkeit gewährleisten.  
111 
Spätere Änderungen sind ausschließlich so vorzunehmen, dass sowohl der ursprüng-
liche Inhalt als auch die Tatsache, dass Veränderungen vorgenommen wurden, 
erkennbar bleiben. Bei programmgenerierten bzw. programmgesteuerten Aufzeich-
nungen (automatisierte Belege bzw. Dauerbelege) sind Änderungen an den der Auf-
zeichnung zugrunde liegenden Generierungs- und Steuerungsdaten ebenfalls aufzu-
zeichnen. Dies betrifft insbesondere die Protokollierung von Änderungen in Einstel-
lungen oder die Parametrisierung der Software. Bei einer Änderung von Stammdaten 


--- Page 28 ---
 
Seite 28
(z. B. Abkürzungs- oder Schlüsselverzeichnisse, Organisationspläne) muss die ein-
deutige Bedeutung in den entsprechenden Bewegungsdaten (z. B. Umsatzsteuer-
schlüssel, Währungseinheit, Kontoeigenschaft) erhalten bleiben. Ggf. müssen Stamm-
datenänderungen ausgeschlossen oder Stammdaten mit Gültigkeitsangaben historisiert 
werden, um mehrdeutige Verknüpfungen zu verhindern. Auch eine Änderungshistorie 
darf nicht nachträglich veränderbar sein. 
112 
Werden Systemfunktionalitäten oder Manipulationsprogramme eingesetzt, die diesen 
Anforderungen entgegenwirken, führt dies zur Ordnungswidrigkeit der elektronischen 
Bücher und sonst erforderlicher elektronischer Aufzeichnungen. 
Beispiel 8: 
 
Einsatz von Zappern, Phantomware, Backofficeprodukten mit dem Ziel unproto-
kollierter Änderungen elektronischer Einnahmenaufzeichnungen. 
9. Aufbewahrung  
113 
Der sachliche Umfang der Aufbewahrungspflicht in § 147 Absatz 1 AO besteht 
grundsätzlich nur im Umfang der Aufzeichnungspflicht (BFH-Urteil vom 24. Juni 
2009, BStBl II 2010 S. 452; BFH-Urteil vom 26. Februar 2004, BStBl II S. 599). 
114 
Müssen Bücher für steuerliche Zwecke geführt werden, sind sie in vollem Umfang 
aufbewahrungs- und vorlagepflichtig (z. B. Finanzbuchhaltung hinsichtlich Drohver-
lustrückstellungen, nicht abziehbare Betriebsausgaben, organschaftliche Steuer-
umlagen; BFH-Beschluss vom 26. September 2007, BStBl II 2008 S. 415). 
115 
Auch Steuerpflichtige, die nach § 4 Absatz 3 EStG als Gewinn den Überschuss der 
Betriebseinnahmen über die Betriebsausgaben ansetzen, sind verpflichtet, Aufzeich-
nungen und Unterlagen nach § 147 Absatz 1 AO aufzubewahren (BFH-Urteil vom 
24. Juni 2009, BStBl II 2010 S. 452; BFH-Urteil vom 26. Februar 2004, BStBl II 
S. 599). 
116 
Aufbewahrungspflichten können sich auch aus anderen Rechtsnormen (z. B. § 14b 
UStG) ergeben. 
117 
Die aufbewahrungspflichtigen Unterlagen müssen geordnet aufbewahrt werden. Ein 
bestimmtes Ordnungssystem ist nicht vorgeschrieben. Die Ablage kann z. B. nach 
Zeitfolge, Sachgruppen, Kontenklassen, Belegnummern oder alphabetisch erfolgen. 
Bei elektronischen Unterlagen ist ihr Eingang, ihre Archivierung und ggf. Konver-
tierung sowie die weitere Verarbeitung zu protokollieren. Es muss jedoch sicherge-
stellt sein, dass ein sachverständiger Dritter innerhalb angemessener Zeit prüfen kann. 
118 
Die nach außersteuerlichen und steuerlichen Vorschriften aufzeichnungspflichtigen 
und nach § 147 Absatz 1 AO aufbewahrungspflichtigen Unterlagen können nach § 147 
Absatz 2 AO bis auf wenige Ausnahmen auch als Wiedergabe auf einem Bildträger 


--- Page 29 ---
 
Seite 29
oder auf anderen Datenträgern aufbewahrt werden, wenn dies den GoB entspricht und 
sichergestellt ist, dass die Wiedergabe oder die Daten 
1. mit den empfangenen Handels- oder Geschäftsbriefen und den Buchungsbelegen 
bildlich und mit den anderen Unterlagen inhaltlich übereinstimmen, wenn sie 
lesbar gemacht werden, 
1. während der Dauer der Aufbewahrungsfrist jederzeit verfügbar sind, unverzüglich 
lesbar gemacht und maschinell ausgewertet werden können. 
119 
Sind aufzeichnungs- und aufbewahrungspflichtige Daten, Datensätze, elektronische 
Dokumente und elektronische Unterlagen im Unternehmen entstanden oder dort ein-
gegangen, sind sie auch in dieser Form aufzubewahren und dürfen vor Ablauf der Auf-
bewahrungsfrist nicht gelöscht werden. Sie dürfen daher nicht mehr ausschließlich in 
ausgedruckter Form aufbewahrt werden und müssen für die Dauer der Aufbewah-
rungsfrist unveränderbar erhalten bleiben (z. B. per E-Mail eingegangene Rechnung im 
PDF-Format oder bildlich erfasste Papierbelege). Dies gilt unabhängig davon, ob die 
Aufbewahrung im Produktivsystem oder durch Auslagerung in ein anderes DV-System 
erfolgt. Unter Zumutbarkeitsgesichtspunkten ist es nicht zu beanstanden, wenn der 
Steuerpflichtige elektronisch erstellte und in Papierform abgesandte Handels- und 
Geschäftsbriefe nur in Papierform aufbewahrt. 
120 
Beispiel 9 zu Rz. 119: 
Ein Steuerpflichtiger erstellt seine Ausgangsrechnungen mit einem Textverarbeitungs-
programm. Nach dem Ausdruck der jeweiligen Rechnung wird die hierfür verwendete 
Maske (Dokumentenvorlage) mit den Inhalten der nächsten Rechnung überschrieben. 
Es ist in diesem Fall nicht zu beanstanden, wenn das Doppel des versendeten Schrei-
bens in diesem Fall nur als Papierdokument aufbewahrt wird. Werden die abgesandten 
Handels- und Geschäftsbriefe jedoch tatsächlich in elektronischer Form aufbewahrt 
(z. B. im File-System oder einem DMS-System), so ist eine ausschließliche Aufbe-
wahrung in Papierform nicht mehr zulässig. Das Verfahren muss dokumentiert wer-
den. Werden Handels- oder Geschäftsbriefe mit Hilfe eines Fakturierungssystems oder 
ähnlicher Anwendungen erzeugt, bleiben die elektronischen Daten aufbewahrungs-
pflichtig. 
121 
Bei den Daten und Dokumenten ist - wie bei den Informationen in Papierbelegen - auf 
deren Inhalt und auf deren Funktion abzustellen, nicht auf deren Bezeichnung. So sind 
beispielsweise E-Mails mit der Funktion eines Handels- oder Geschäftsbriefs oder 
eines Buchungsbelegs in elektronischer Form aufbewahrungspflichtig. Dient eine 
E-Mail nur als „Transportmittel“, z. B. für eine angehängte elektronische Rechnung, 
und enthält darüber hinaus keine weitergehenden aufbewahrungspflichtigen Informa-
tionen, so ist diese nicht aufbewahrungspflichtig (wie der bisherige Papierbriefum-
schlag). 


--- Page 30 ---
 
Seite 30
122 
Ein elektronisches Dokument ist mit einem nachvollziehbaren und eindeutigen Index 
zu versehen. Der Erhalt der Verknüpfung zwischen Index und elektronischem Doku-
ment muss während der gesamten Aufbewahrungsfrist gewährleistet sein. Es ist 
sicherzustellen, dass das elektronische Dokument unter dem zugeteilten Index ver-
waltet werden kann. Stellt ein Steuerpflichtiger durch organisatorische Maßnahmen 
sicher, dass das elektronische Dokument auch ohne Index verwaltet werden kann, und 
ist dies in angemessener Zeit nachprüfbar, so ist aus diesem Grund die Buchführung 
nicht zu beanstanden.  
123 
Das Anbringen von Buchungsvermerken, Indexierungen, Barcodes, farblichen Hervor-
hebungen usw. darf - unabhängig von seiner technischen Ausgestaltung - keinen Ein-
fluss auf die Lesbarmachung des Originalzustands haben. Die elektronischen Bearbei-
tungsvorgänge sind zu protokollieren und mit dem elektronischen Dokument zu 
speichern, damit die Nachvollziehbarkeit und Prüfbarkeit des Originalzustands und 
seiner Ergänzungen gewährleistet ist. 
124 
Hinsichtlich der Aufbewahrung digitaler Unterlagen bei Bargeschäften wird auf das 
BMF-Schreiben vom 26. November 2010 (IV A 4 - S 0316/08/10004-07, BStBl I 
S. 1342) hingewiesen. 
9.1 Maschinelle Auswertbarkeit (§ 147 Absatz 2 Nummer 2 AO) 
125 
Art und Umfang der maschinellen Auswertbarkeit sind nach den tatsächlichen 
Informations- und Dokumentationsmöglichkeiten zu beurteilen. 
Beispiel 10: 
Datenformat für elektronische Rechnungen ZUGFeRD (Zentraler User Guide des 
Forums elektronische Rechnung Deutschland) 
Hier ist vorgesehen, dass Rechnungen im PDF/A-3-Format versendet werden. Diese 
bestehen aus einem Rechnungsbild (dem augenlesbaren, sichtbaren Teil der PDF-
Datei) und den in die PDF-Datei eingebetteten Rechnungsdaten im standardisierten 
XML-Format. 
Entscheidend ist hier jetzt nicht, ob der Rechnungsempfänger nur das Rechnungsbild 
(Image) nutzt, sondern, dass auch noch tatsächlich XML-Daten vorhanden sind, die 
nicht durch eine Formatumwandlung (z. B. in TIFF) gelöscht werden dürfen.  
Die maschinelle Auswertbarkeit bezieht sich auf sämtliche Inhalte der PDF/A-3-Datei. 
126 
Eine maschinelle Auswertbarkeit ist nach diesem Beurteilungsmaßstab bei aufzeich-
nungs- und aufbewahrungspflichtigen Daten, Datensätzen, elektronischen Dokumen-
ten und elektronischen Unterlagen (vgl. Rzn. 3 bis 5) u. a. gegeben, die 
• mathematisch-technische Auswertungen ermöglichen, 
• eine Volltextsuche ermöglichen, 
• auch ohne mathematisch-technische Auswertungen eine Prüfung im weitesten 


--- Page 31 ---
 
Seite 31
Sinne ermöglichen (z. B. Bildschirmabfragen, die Nachverfolgung von 
Verknüpfungen und Verlinkungen oder die Textsuche nach bestimmten 
Eingabekriterien).  
127 
Mathematisch-technische Auswertung bedeutet, dass alle in den aufzeichnungs- und 
aufbewahrungspflichtigen Daten, Datensätzen, elektronischen Dokumenten und 
elektronischen Unterlagen (vgl. Rzn. 3 bis 5) enthaltenen Informationen automatisiert 
(DV-gestützt) interpretiert, dargestellt, verarbeitet sowie für andere Datenbank-
anwendungen und eingesetzte Prüfsoftware direkt, ohne weitere Konvertierungs- und 
Bearbeitungsschritte und ohne Informationsverlust nutzbar gemacht werden können 
(z. B. für wahlfreie Sortier-, Summier-, Verbindungs- und Filterungsmöglichkeiten). 
Mathematisch-technische Auswertungen sind z. B. möglich bei: 
• Elektronischen Grund(buch)aufzeichnungen (z. B. Kassendaten, Daten aus Waren-
wirtschaftssystem, Inventurlisten), 
• Journaldaten aus Finanzbuchhaltung oder Lohnbuchhaltung, 
• Textdateien oder Dateien aus Tabellenkalkulationen mit strukturierten Daten in 
tabellarischer Form (z. B. Reisekostenabrechnung, Überstundennachweise). 
128 
Neben den Daten in Form von Datensätzen und den elektronischen Dokumenten sind 
auch alle zur maschinellen Auswertung der Daten im Rahmen des Datenzugriffs not-
wendigen Strukturinformationen (z. B. über die Dateiherkunft [eingesetztes System], 
die Dateistruktur, die Datenfelder, verwendete Zeichensatztabellen) in maschinell 
auswertbarer Form sowie die internen und externen Verknüpfungen vollständig und in 
unverdichteter, maschinell auswertbarer Form aufzubewahren. Im Rahmen einer 
Datenträgerüberlassung ist der Erhalt technischer Verlinkungen auf dem Datenträger 
nicht erforderlich, sofern dies nicht möglich ist. 
129 
Die Reduzierung einer bereits bestehenden maschinellen Auswertbarkeit, beispiels-
weise durch Umwandlung des Dateiformats oder der Auswahl bestimmter Aufbewah-
rungsformen, ist nicht zulässig (siehe unter 9.2). 
Beispiele 11: 
• Umwandlung von PDF/A-Dateien ab der Norm PDF/A-3 in ein Bildformat (z. B. 
TIFF, JPEG etc.), da dann die in den PDF/A-Dateien enthaltenen XML-Daten und 
ggf. auch vorhandene Volltextinformationen gelöscht werden. 
• Umwandlung von elektronischen Grund(buch)aufzeichnungen (z. B. Kasse, 
Warenwirtschaft) in ein PDF-Format. 
• Umwandlung von Journaldaten einer Finanzbuchhaltung oder Lohnbuchhaltung in 
ein PDF-Format. 
Eine Umwandlung in ein anderes Format (z. B. Inhouse-Format) ist zulässig, wenn die 
maschinelle Auswertbarkeit nicht eingeschränkt wird und keine inhaltliche Verände-
rung vorgenommen wird (siehe Rz. 135). 


--- Page 32 ---
 
Seite 32
Der Steuerpflichtige muss dabei auch berücksichtigen, dass entsprechende Einschrän-
kungen in diesen Fällen zu seinen Lasten gehen können (z. B. Speicherung einer 
E-Mail als PDF-Datei. Die Informationen des Headers [z. B. Informationen zum 
Absender] gehen dabei verloren und es ist nicht mehr nachvollziehbar, wie der tat-
sächliche Zugang der E-Mail erfolgt ist). 
9.2 Elektronische Aufbewahrung 
130 
Werden Handels- oder Geschäftsbriefe und Buchungsbelege in Papierform empfangen 
und danach elektronisch bildlich erfasst (z. B. gescannt oder fotografiert), ist das 
hierdurch entstandene elektronische Dokument so aufzubewahren, dass die Wieder-
gabe mit dem Original bildlich übereinstimmt, wenn es lesbar gemacht wird (§ 147 
Absatz  2 AO). Eine bildliche Erfassung kann hierbei mit den verschiedensten Arten 
von Geräten (z. B. Smartphones, Multifunktionsgeräten oder Scan-Straßen) erfolgen, 
wenn die Anforderungen dieses Schreibens erfüllt sind. Werden bildlich erfasste 
Dokumente per Optical-Character-Recognition-Verfahren (OCR-Verfahren) um 
Volltextinformationen angereichert (zum Beispiel volltextrecherchierbare PDFs), so 
ist dieser Volltext nach Verifikation und Korrektur über die Dauer der Aufbewah-
rungsfrist aufzubewahren und auch für Prüfzwecke verfügbar zu machen. § 146 
Absatz 2 AO steht einer bildlichen Erfassung durch mobile Geräte (z. B. Smartphones) 
im Ausland nicht entgegen, wenn die Belege im Ausland entstanden sind bzw. 
empfangen wurden und dort direkt erfasst werden (z. B. bei Belegen über eine 
Dienstreise im Ausland). 
131 
Eingehende elektronische Handels- oder Geschäftsbriefe und Buchungsbelege müssen 
in dem Format aufbewahrt werden, in dem sie empfangen wurden (z. B. Rechnungen 
oder Kontoauszüge im PDF- oder Bildformat). Eine Umwandlung in ein anderes 
Format (z. B. MSG in PDF) ist dann zulässig, wenn die maschinelle Auswertbarkeit 
nicht eingeschränkt wird und keine inhaltlichen Veränderungen vorgenommen werden 
(siehe Rz. 135). Erfolgt eine Anreicherung der Bildinformationen, z. B. durch OCR 
(Beispiel: Erzeugung einer volltextrecherchierbaren PDF-Datei im Erfassungsprozess), 
sind die dadurch gewonnenen Informationen nach Verifikation und Korrektur 
ebenfalls aufzubewahren. 
132 
Im DV-System erzeugte Daten im Sinne der Rzn. 3 bis 5 (z. B. Grund(buch)aufzeich-
nungen in Vor- und Nebensystemen, Buchungen, generierte Datensätze zur Erstellung 
von Ausgangsrechnungen) oder darin empfangene Daten (z. B. EDI-Verfahren) 
müssen im Ursprungsformat aufbewahrt werden. 
133 
Im DV-System erzeugte Dokumente (z. B. als Textdokumente erstellte Ausgangs-
rechnungen [§ 14b UStG], elektronisch abgeschlossene Verträge, Handels- und 
Geschäftsbriefe, Verfahrensdokumentation) sind im Ursprungsformat aufzubewahren. 


--- Page 33 ---
 
Seite 33
Unter Zumutbarkeitsgesichtspunkten ist es nicht zu beanstanden, wenn der Steuer-
pflichtige elektronisch erstellte und in Papierform abgesandte Handels- und Geschäfts-
briefe nur in Papierform aufbewahrt (Hinweis auf Rzn. 119, 120). Eine Umwandlung 
in ein anderes Format (z. B. Inhouse-Format) ist zulässig, wenn die maschinelle Aus-
wertbarkeit nicht eingeschränkt wird und keine inhaltliche Veränderung vorgenommen 
wird (siehe Rz. 135).  
134 
Bei Einsatz von Kryptografietechniken ist sicherzustellen, dass die verschlüsselten 
Unterlagen im DV-System in entschlüsselter Form zur Verfügung stehen.  
Werden Signaturprüfschlüssel verwendet, sind die eingesetzten Schlüssel aufzu-
bewahren. Die Aufbewahrungspflicht endet, wenn keine der mit den Schlüsseln 
signierten Unterlagen mehr aufbewahrt werden müssen. 
135 
Bei Umwandlung (Konvertierung) aufbewahrungspflichtiger Unterlagen in ein unter-
nehmenseigenes Format (sog. Inhouse-Format) sind beide Versionen zu archivieren, 
derselben Aufzeichnung zuzuordnen und mit demselben Index zu verwalten sowie die 
konvertierte Version als solche zu kennzeichnen.  
Die Aufbewahrung beider Versionen ist bei Beachtung folgender Anforderungen nicht 
erforderlich, sondern es ist die Aufbewahrung der konvertierten Fassung ausreichend: 
• Es wird keine bildliche oder inhaltliche Veränderung vorgenommen. 
• Bei der Konvertierung gehen keine sonstigen aufbewahrungspflichtigen 
Informationen verloren. 
• Die ordnungsgemäße und verlustfreie Konvertierung wird dokumentiert 
(Verfahrensdokumentation). 
• Die maschinelle Auswertbarkeit und der Datenzugriff durch die Finanzbehörde 
werden nicht eingeschränkt; dabei ist es zulässig, wenn bei der Konvertierung 
Zwischenaggregationsstufen nicht gespeichert, aber in der Verfahrensdokumen-
tation so dargestellt werden, dass die retrograde und progressive Prüfbarkeit 
sichergestellt ist. 
 
Nicht aufbewahrungspflichtig sind die während der maschinellen Verarbeitung durch 
das Buchführungssystem erzeugten Dateien, sofern diese ausschließlich einer 
temporären Zwischenspeicherung von Verarbeitungsergebnissen dienen und deren 
Inhalte im Laufe des weiteren Verarbeitungsprozesses vollständig Eingang in die 
Buchführungsdaten finden. Voraussetzung ist jedoch, dass bei der weiteren Verarbei-
tung keinerlei „Verdichtung“ aufzeichnungs- und aufbewahrungspflichtiger Daten 
(vgl. Rzn. 3 bis 5) vorgenommen wird. 


--- Page 34 ---
 
Seite 34
9.3 Bildliche Erfassung von Papierdokumenten  
136 
Papierdokumente werden durch die bildliche Erfassung (siehe Rz. 130) in elektroni-
sche Dokumente umgewandelt. Das Verfahren muss dokumentiert werden.  
Der Steuerpflichtige sollte daher eine Organisationsanweisung erstellen, die unter 
anderem regelt: 
• wer erfassen darf, 
• zu welchem Zeitpunkt erfasst wird oder erfasst werden soll (z. B. beim 
Posteingang, während oder nach Abschluss der Vorgangsbearbeitung), 
• welches Schriftgut erfasst wird, 
• ob eine bildliche oder inhaltliche Übereinstimmung mit dem Original erforderlich ist,  
• wie die Qualitätskontrolle auf Lesbarkeit und Vollständigkeit und 
• wie die Protokollierung von Fehlern zu erfolgen hat. 
 
Die konkrete Ausgestaltung dieser Verfahrensdokumentation ist abhängig von der 
Komplexität und Diversifikation der Geschäftstätigkeit und der Organisationsstruktur 
sowie des eingesetzten DV-Systems.  
Aus Vereinfachungsgründen (z. B. bei Belegen über eine Dienstreise im Ausland) 
steht § 146 Absatz 2 AO einer bildlichen Erfassung durch mobile Geräte (z. B. 
Smartphones) im Ausland nicht entgegen, wenn die Belege im Ausland entstanden 
sind bzw. empfangen wurden und dort direkt erfasst werden.  
Erfolgt im Zusammenhang mit einer, nach § 146 Absatz 2a AO genehmigten, 
Verlagerung der elektronischen Buchführung ins Ausland eine ersetzende bildliche 
Erfassung, wird es nicht beanstandet, wenn die papierenen Ursprungsbelege zu diesem 
Zweck an den Ort der elektronischen Buchführung verbracht werden. Die bildliche 
Erfassung hat zeitnah zur Verbringung der Papierbelege ins Ausland zu erfolgen. 
 
137 
Eine vollständige Farbwiedergabe ist erforderlich, wenn der Farbe Beweisfunktion 
zukommt (z. B. Minusbeträge in roter Schrift, Sicht-, Bearbeitungs- und Zeichnungs-
vermerke in unterschiedlichen Farben). 
 
138 
Für Besteuerungszwecke ist eine elektronische Signatur oder ein Zeitstempel nicht 
erforderlich.  
139 
Im Anschluss an den Erfassungsvorgang (siehe Rz. 130) darf die weitere Bearbeitung 
nur mit dem elektronischen Dokument erfolgen. Die Papierbelege sind dem weiteren 
Bearbeitungsgang zu entziehen, damit auf diesen keine Bemerkungen, Ergänzungen 
usw. vermerkt werden können, die auf dem elektronischen Dokument nicht enthalten 
sind. Sofern aus organisatorischen Gründen nach dem Erfassungsvorgang eine weitere 
Vorgangsbearbeitung des Papierbeleges erfolgt, muss nach Abschluss der Bearbeitung 


--- Page 35 ---
 
Seite 35
der bearbeitete Papierbeleg erneut erfasst und ein Bezug zur ersten elektronischen 
Fassung des Dokuments hergestellt werden (gemeinsamer Index).  
140 
Nach der bildlichen Erfassung im Sinne der Rz. 130 dürfen Papierdokumente vernich-
tet werden, soweit sie nicht nach außersteuerlichen oder steuerlichen Vorschriften im 
Original aufzubewahren sind. Der Steuerpflichtige muss entscheiden, ob Dokumente, 
deren Beweiskraft bei der Aufbewahrung in elektronischer Form nicht erhalten bleibt, 
zusätzlich in der Originalform aufbewahrt werden sollen.  
141 
Der Verzicht auf einen Papierbeleg darf die Möglichkeit der Nachvollziehbarkeit und 
Nachprüfbarkeit nicht beeinträchtigen. 
9.4 Auslagerung von Daten aus dem Produktivsystem und Systemwechsel 
142 
Im Falle eines Systemwechsels (z. B. Abschaltung Altsystem, Datenmigration), einer 
Systemänderung (z. B. Änderung der OCR-Software, Update der Finanzbuchhaltung 
etc.) oder einer Auslagerung von aufzeichnungs- und aufbewahrungspflichtigen Daten 
(vgl. Rzn. 3 bis 5) aus dem Produktivsystem ist es nur dann nicht erforderlich, die 
ursprüngliche Hard- und Software des Produktivsystems über die Dauer der Aufbe-
wahrungsfrist vorzuhalten, wenn die folgenden Voraussetzungen erfüllt sind: 
1. Die aufzeichnungs- und aufbewahrungspflichtigen Daten (einschließlich 
Metadaten, Stammdaten, Bewegungsdaten und der erforderlichen 
Verknüpfungen) müssen unter Beachtung der Ordnungsvorschriften (vgl. 
§§ 145 bis 147 AO) quantitativ und qualitativ gleichwertig in ein neues System, 
in eine neue Datenbank, in ein Archivsystem oder in ein anderes System 
überführt werden.  
Bei einer erforderlichen Datenumwandlung (Migration) darf ausschließlich das 
Format der Daten (z. B. Datums- und Währungsformat) umgesetzt, nicht aber 
eine inhaltliche Änderung der Daten vorgenommen werden. Die vorgenomme-
nen Änderungen sind zu dokumentieren.  
Die Reorganisation von OCR-Datenbanken ist zulässig, soweit die zugrunde 
liegenden elektronischen Dokumente und Unterlagen durch diesen Vorgang 
unverändert bleiben und die durch das OCR-Verfahren gewonnenen 
Informationen mindestens in quantitativer und qualitativer Hinsicht erhalten 
bleiben. 
1. Das neue System, das Archivsystem oder das andere System muss in quantitati-
ver und qualitativer Hinsicht die gleichen Auswertungen der aufzeichnungs- 
und aufbewahrungspflichtigen Daten ermöglichen als wären die Daten noch im 
Produktivsystem. 
 


--- Page 36 ---
 
Seite 36
143 
Andernfalls ist die ursprüngliche Hard- und Software des Produktivsystems - neben 
den aufzeichnungs- und aufbewahrungspflichtigen Daten - für die Dauer der Aufbe-
wahrungsfrist vorzuhalten. Auf die Möglichkeit der Bewilligung von Erleichterungen 
nach § 148 AO wird hingewiesen. 
144 
Eine Aufbewahrung in Form von Datenextrakten, Reports oder Druckdateien ist 
unzulässig, soweit nicht mehr alle aufzeichnungs- und aufbewahrungspflichtigen 
Daten übernommen werden. 
10. Nachvollziehbarkeit und Nachprüfbarkeit 
145 
Die allgemeinen Grundsätze der Nachvollziehbarkeit und Nachprüfbarkeit sind unter 
3.1 aufgeführt.  
Die Prüfbarkeit der formellen und sachlichen Richtigkeit bezieht sich sowohl auf 
einzelne Geschäftsvorfälle (Einzelprüfung) als auch auf die Prüfbarkeit des gesamten 
Verfahrens (Verfahrens- oder Systemprüfung anhand einer Verfahrensdokumentation, 
siehe unter 10.1).  
146 
Auch an die DV-gestützte Buchführung wird die Anforderung gestellt, dass Geschäfts-
vorfälle für die Dauer der Aufbewahrungsfrist retrograd und progressiv prüfbar 
bleiben müssen.  
147 
Die vorgenannten Anforderungen gelten für sonst erforderliche elektronische Auf-
zeichnungen sinngemäß (§ 145 Absatz 2 AO).  
148 
Von einem sachverständigen Dritten kann zwar Sachverstand hinsichtlich der 
Ordnungsvorschriften der §§ 145 bis 147 AO und allgemeiner DV-Sachverstand 
erwartet werden, nicht jedoch spezielle, produktabhängige System- oder Programmier-
kenntnisse.  
149 
Nach § 146 Absatz 3 Satz 3 AO muss im Einzelfall die Bedeutung von Abkürzungen, 
Ziffern, Buchstaben und Symbolen eindeutig festliegen und sich aus der Verfahrens-
dokumentation ergeben.  
150 
Für die Prüfung ist eine aussagefähige und aktuelle Verfahrensdokumentation 
notwendig, die alle System- bzw. Verfahrensänderungen inhaltlich und zeitlich 
lückenlos dokumentiert. 
10.1 
Verfahrensdokumentation 
151 
Da sich die Ordnungsmäßigkeit neben den elektronischen Büchern und sonst erforder-
lichen Aufzeichnungen auch auf die damit in Zusammenhang stehenden Verfahren 
und Bereiche des DV-Systems bezieht (siehe unter 3.), muss für jedes DV-System eine 
übersichtlich gegliederte Verfahrensdokumentation vorhanden sein, aus der Inhalt, 


--- Page 37 ---
 
Seite 37
Aufbau, Ablauf und Ergebnisse des DV-Verfahrens vollständig und schlüssig ersicht-
lich sind. Der Umfang der im Einzelfall erforderlichen Dokumentation wird dadurch 
bestimmt, was zum Verständnis des DV-Verfahrens, der Bücher und Aufzeichnungen 
sowie der aufbewahrten Unterlagen notwendig ist. Die Verfahrensdokumentation muss 
verständlich und damit für einen sachverständigen Dritten in angemessener Zeit nach-
prüfbar sein. Die konkrete Ausgestaltung der Verfahrensdokumentation ist abhängig 
von der Komplexität und Diversifikation der Geschäftstätigkeit und der Organisations-
struktur sowie des eingesetzten DV-Systems.  
152 
Die Verfahrensdokumentation beschreibt den organisatorisch und technisch gewollten 
Prozess, z. B. bei elektronischen Dokumenten von der Entstehung der Informationen 
über die Indizierung, Verarbeitung und Speicherung, dem eindeutigen Wiederfinden 
und der maschinellen Auswertbarkeit, der Absicherung gegen Verlust und Verfäl-
schung und der Reproduktion.  
153 
Die Verfahrensdokumentation besteht in der Regel aus einer allgemeinen Beschrei-
bung, einer Anwenderdokumentation, einer technischen Systemdokumentation und 
einer Betriebsdokumentation.  
154 
Für den Zeitraum der Aufbewahrungsfrist muss gewährleistet und nachgewiesen sein, 
dass das in der Dokumentation beschriebene Verfahren dem in der Praxis eingesetzten 
Verfahren voll entspricht. Dies gilt insbesondere für die eingesetzten Versionen der 
Programme (Programmidentität). Änderungen einer Verfahrensdokumentation müssen 
historisch nachvollziehbar sein. Dem wird genügt, wenn die Änderungen versioniert 
sind und eine nachvollziehbare Änderungshistorie vorgehalten wird. Aus der Verfah-
rensdokumentation muss sich ergeben, wie die Ordnungsvorschriften (z. B. §§ 145 ff. 
AO, §§ 238 ff. HGB) und damit die in diesem Schreiben enthaltenen Anforderungen 
beachtet werden. Die Aufbewahrungsfrist für die Verfahrensdokumentation läuft nicht 
ab, soweit und solange die Aufbewahrungsfrist für die Unterlagen noch nicht abgelaufen 
ist, zu deren Verständnis sie erforderlich ist.  
155 
Soweit eine fehlende oder ungenügende Verfahrensdokumentation die Nachvoll-
ziehbarkeit und Nachprüfbarkeit nicht beeinträchtigt, liegt kein formeller Mangel mit 
sachlichem Gewicht vor, der zum Verwerfen der Buchführung führen kann. 
10.2 
Lesbarmachung von elektronischen Unterlagen 
156 
Wer aufzubewahrende Unterlagen in der Form einer Wiedergabe auf einem Bildträger 
oder auf anderen Datenträgern vorlegt, ist nach § 147 Absatz 5 AO verpflichtet, auf 
seine Kosten diejenigen Hilfsmittel zur Verfügung zu stellen, die erforderlich sind, um 
die Unterlagen lesbar zu machen. Auf Verlangen der Finanzbehörde hat der Steuer-
pflichtige auf seine Kosten die Unterlagen unverzüglich ganz oder teilweise auszu-
drucken oder ohne Hilfsmittel lesbare Reproduktionen beizubringen. 


--- Page 38 ---
 
Seite 38
157 
Der Steuerpflichtige muss durch Erfassen im Sinne der Rz. 130 digitalisierte Unter-
lagen über sein DV-System per Bildschirm lesbar machen. Ein Ausdruck auf Papier ist 
nicht ausreichend. Die elektronischen Dokumente müssen für die Dauer der Aufbe-
wahrungsfrist jederzeit lesbar sein (BFH-Beschluss vom 26. September 2007, BStBl II 
2008 S. 415). 
11. Datenzugriff 
158 
Die Finanzbehörde hat das Recht, die mit Hilfe eines DV-Systems erstellten und nach 
§ 147 Absatz 1 AO aufbewahrungspflichtigen Unterlagen durch Datenzugriff zu 
prüfen. Das Recht auf Datenzugriff steht der Finanzbehörde nur im Rahmen der 
gesetzlichen Regelungen zu (z.B. Außenprüfung und Nachschauen). Durch die 
Regelungen zum Datenzugriff wird der sachliche Umfang der Außenprüfung (§ 194 
AO) nicht erweitert; er wird durch die Prüfungsanordnung (§ 196 AO, § 5 BpO) 
bestimmt.  
11.1 
Umfang und Ausübung des Rechts auf Datenzugriff nach § 147 Absatz 6 
AO 
159 
Gegenstand der Prüfung sind die nach außersteuerlichen und steuerlichen Vorschriften 
aufzeichnungspflichtigen und die nach § 147 Absatz 1 AO aufbewahrungspflichtigen 
Unterlagen. Hierfür sind insbesondere die Daten der Finanzbuchhaltung, der Anlagen-
buchhaltung, der Lohnbuchhaltung und aller Vor- und Nebensysteme, die aufzeich-
nungs- und aufbewahrungspflichtige Unterlagen enthalten (vgl. Rzn. 3 bis 5), für den 
Datenzugriff bereitzustellen. Die Art der Außenprüfung ist hierbei unerheblich, so 
dass z. B. die Daten der Finanzbuchhaltung auch Gegenstand der Lohnsteuer-Außen-
prüfung sein können. 
160 
Neben den Daten müssen insbesondere auch die Teile der Verfahrensdokumentation 
auf Verlangen zur Verfügung gestellt werden können, die einen vollständigen 
Systemüberblick ermöglichen und für das Verständnis des DV-Systems erforderlich 
sind. Dazu gehört auch ein Überblick über alle im DV-System vorhandenen Informa-
tionen, die aufzeichnungs- und aufbewahrungspflichtige Unterlagen betreffen (vgl. 
Rzn. 3 bis 5); z. B. Beschreibungen zu Tabellen, Feldern, Verknüpfungen und 
Auswertungen. Diese Angaben sind erforderlich, damit die Finanzverwaltung das 
durch den Steuerpflichtigen ausgeübte Erstqualifikationsrecht (vgl. Rz. 161) prüfen 
und Aufbereitungen für die Datenträgerüberlassung erstellen kann. 
161 
Soweit in Bereichen des Unternehmens betriebliche Abläufe mit Hilfe eines DV-
Systems abgebildet werden, sind die betroffenen DV-Systeme durch den Steuer-
pflichtigen zu identifizieren, die darin enthaltenen Daten nach Maßgabe der außer-
steuerlichen und steuerlichen Aufzeichnungs- und Aufbewahrungspflichten 


--- Page 39 ---
 
Seite 39
(vgl. Rzn. 3 bis 5) zu qualifizieren (Erstqualifizierung) und für den Datenzugriff in 
geeigneter Weise vorzuhalten (siehe auch unter 9.4). Bei unzutreffender Qualifi-
zierung von Daten kann die Finanzbehörde im Rahmen ihres pflichtgemäßen 
Ermessens verlangen, dass der Steuerpflichtige den Datenzugriff auf diese nach 
außersteuerlichen und steuerlichen Vorschriften tatsächlich aufgezeichneten und 
aufbewahrten Daten nachträglich ermöglicht.  
Beispiele 12: 
• Ein Steuerpflichtiger stellt aus dem PC-Kassensystem nur Tagesendsummen zur 
Verfügung. Die digitalen Grund(buch)aufzeichnungen (Kasseneinzeldaten) wur-
den archiviert, aber nicht zur Verfügung gestellt. 
• Ein Steuerpflichtiger stellt für die Datenträgerüberlassung nur einzelne Sachkonten 
aus der Finanzbuchhaltung zur Verfügung. Die Daten der Finanzbuchhaltung sind 
archiviert. 
• Ein Steuerpflichtiger ohne Auskunftsverweigerungsrecht stellt Belege in Papier-
form zur Verfügung. Die empfangenen und abgesandten Handels- und Geschäfts-
briefe und Buchungsbelege stehen in einem Dokumenten-Management-System zur 
Verfügung. 
162 
Das allgemeine Auskunftsrecht des Prüfers (§§ 88, 199 Absatz 1 AO) und die 
Mitwirkungspflichten des Steuerpflichtigen (§§ 90, 200 AO) bleiben unberührt. 
163 
Bei der Ausübung des Rechts auf Datenzugriff stehen der Finanzbehörde nach dem 
Gesetz drei gleichberechtigte Möglichkeiten zur Verfügung.  
164 
Die Entscheidung, von welcher Möglichkeit des Datenzugriffs die Finanzbehörde 
Gebrauch macht, steht in ihrem pflichtgemäßen Ermessen; falls erforderlich, kann sie 
auch kumulativ mehrere Möglichkeiten in Anspruch nehmen (Rzn. 165 bis 170). 
Sofern noch nicht mit der Außenprüfung begonnen wurde, ist es im Falle eines 
Systemwechsels oder einer Auslagerung von aufzeichnungs- und aufbewahrungs-
pflichtigen Daten aus dem Produktivsystem ausreichend, wenn nach Ablauf des 
5. Kalenderjahres, das auf die Umstellung folgt, nur noch der Z3-Zugriff (Rzn. 167 bis 
170) zur Verfügung gestellt wird. 
 
165 
Unmittelbarer Datenzugriff (Z1) 
Die Finanzbehörde hat das Recht, selbst unmittelbar auf das DV-System dergestalt 
zuzugreifen, dass sie in Form des Nur-Lesezugriffs Einsicht in die aufzeichnungs- und 
aufbewahrungspflichtigen Daten nimmt und die vom Steuerpflichtigen oder von einem 
beauftragten Dritten eingesetzte Hard- und Software zur Prüfung der gespeicherten 
Daten einschließlich der jeweiligen Meta-, Stamm- und Bewegungsdaten sowie der 
entsprechenden Verknüpfungen (z. B. zwischen den Tabellen einer relationalen 
Datenbank) nutzt.  


--- Page 40 ---
 
Seite 40
Dabei darf sie nur mit Hilfe dieser Hard- und Software auf die elektronisch gespei-
cherten Daten zugreifen. Dies schließt eine Fernabfrage (Online-Zugriff) der 
Finanzbehörde auf das DV-System des Steuerpflichtigen durch die Finanzbehörde aus. 
Der Nur-Lesezugriff umfasst das Lesen und Analysieren der Daten unter Nutzung der 
im DV-System vorhandenen Auswertungsmöglichkeiten (z. B. Filtern und Sortieren). 
166 
Mittelbarer Datenzugriff (Z2) 
Die Finanzbehörde kann vom Steuerpflichtigen auch verlangen, dass er an ihrer Stelle 
die aufzeichnungs- und aufbewahrungspflichtigen Daten nach ihren Vorgaben 
maschinell auswertet oder von einem beauftragten Dritten maschinell auswerten lässt, 
um anschließend einen Nur-Lesezugriff durchführen zu können. Es kann nur eine 
maschinelle Auswertung unter Verwendung der im DV-System des Steuerpflichtigen 
oder des beauftragten Dritten vorhandenen Auswertungsmöglichkeiten verlangt 
werden. 
167 
Datenträgerüberlassung (Z3) 
Die Finanzbehörde kann ferner verlangen, dass ihr die aufzeichnungs- und aufbewah-
rungspflichtigen Daten, einschließlich der jeweiligen Meta-, Stamm- und Bewegungs-
daten sowie der internen und externen Verknüpfungen (z. B. zwischen den Tabellen 
einer relationalen Datenbank), und elektronische Dokumente und Unterlagen auf 
einem maschinell lesbaren und auswertbaren Datenträger zur Auswertung überlassen 
werden. Die Finanzbehörde ist nicht berechtigt, selbst Daten aus dem DV-System 
herunterzuladen oder Kopien vorhandener Datensicherungen vorzunehmen. 
168 
Die Datenträgerüberlassung umfasst die Mitnahme der Daten aus der Sphäre des 
Steuerpflichtigen. Eine Mitnahme der Datenträger aus der Sphäre des Steuerpflich-
tigen sollte im Regelfall nur in Abstimmung mit dem Steuerpflichtigen erfolgen. 
169 
Der zur Auswertung überlassene Datenträger ist spätestens nach Bestandskraft der 
aufgrund der Außenprüfung ergangenen Bescheide an den Steuerpflichtigen zurück-
zugeben und die Daten sind zu löschen. 
170 
Die Finanzbehörde hat bei Anwendung der Regelungen zum Datenzugriff den Grund-
satz der Verhältnismäßigkeit zu beachten. 
11.2 
Umfang der Mitwirkungspflicht nach §§ 147 Absatz 6 und 200 Absatz 1 
Satz 2 AO 
171 
Der Steuerpflichtige hat die Finanzbehörde bei Ausübung ihres Rechts auf Datenzu-
griff zu unterstützen (§ 200 Absatz 1 AO). Dabei entstehende Kosten hat der Steuer-
pflichtige zu tragen (§ 147 Absatz 6 Satz 3 AO). 
172 
Enthalten elektronisch gespeicherte Datenbestände z. B. nicht aufzeichnungs- und auf-
bewahrungspflichtige, personenbezogene oder dem Berufsgeheimnis (§ 102 AO) 


--- Page 41 ---
 
Seite 41
unterliegende Daten, so obliegt es dem Steuerpflichtigen oder dem von ihm beauftrag-
ten Dritten, die Datenbestände so zu organisieren, dass der Prüfer nur auf die auf-
zeichnungs- und aufbewahrungspflichtigen Daten des Steuerpflichtigen zugreifen 
kann. Dies kann z. B. durch geeignete Zugriffsbeschränkungen oder „digitales 
Schwärzen“ der zu schützenden Informationen erfolgen. Für versehentlich überlassene 
Daten besteht kein Verwertungsverbot. 
173 
Mangels Nachprüfbarkeit akzeptiert die Finanzbehörde keine Reports oder Druck-
dateien, die vom Unternehmen ausgewählte („vorgefilterte“) Datenfelder und -sätze 
aufführen, jedoch nicht mehr alle aufzeichnungs- und aufbewahrungspflichtigen Daten 
(vgl. Rzn. 3 bis 5) enthalten.  
Im Einzelnen gilt Folgendes: 
174 
Beim unmittelbaren Datenzugriff hat der Steuerpflichtige dem Prüfer die für den 
Datenzugriff erforderlichen Hilfsmittel zur Verfügung zu stellen und ihn für den Nur-
Lesezugriff in das DV-System einzuweisen. Die Zugangsberechtigung muss so aus-
gestaltet sein, dass dem Prüfer dieser Zugriff auf alle aufzeichnungs- und aufbewah-
rungspflichtigen Daten eingeräumt wird. Sie umfasst die im DV-System genutzten 
Auswertungsmöglichkeiten (z. B. Filtern, Sortieren, Konsolidieren) für Prüfungs-
zwecke (z. B. in Revisionstools, Standardsoftware, Backofficeprodukten). In Abhän-
gigkeit vom konkreten Sachverhalt kann auch eine vom Steuerpflichtigen nicht 
genutzte, aber im DV-System vorhandene Auswertungsmöglichkeit verlangt werden.  
Eine Volltextsuche, eine Ansichtsfunktion oder ein selbsttragendes System, das in 
einer Datenbank nur die für archivierte Dateien vergebenen Schlagworte als Index-
werte nachweist, reicht regelmäßig nicht aus. 
Eine Unveränderbarkeit des Datenbestandes und des DV-Systems durch die Finanz-
behörde muss seitens des Steuerpflichtigen oder eines von ihm beauftragten Dritten 
gewährleistet werden. 
175 
Beim mittelbaren Datenzugriff gehört zur Mithilfe des Steuerpflichtigen beim Nur-
Lesezugriff neben der Zurverfügungstellung von Hard- und Software die Unter-
stützung durch mit dem DV-System vertraute Personen. Der Umfang der zumutbaren 
Mithilfe richtet sich nach den betrieblichen Gegebenheiten des Unternehmens.  
Hierfür können z. B. seine Größe oder Mitarbeiterzahl Anhaltspunkte sein. 
176 
Bei der Datenträgerüberlassung sind der Finanzbehörde mit den gespeicherten Unter-
lagen und Aufzeichnungen alle zur Auswertung der Daten notwendigen Informationen 
(z. B. über die Dateiherkunft [eingesetztes System], die Dateistruktur, die Datenfelder, 
verwendete Zeichensatztabellen sowie interne und externe Verknüpfungen) in 
maschinell auswertbarer Form zur Verfügung zu stellen. Dies gilt auch in den Fällen, 
in denen sich die Daten bei einem Dritten befinden. 
Auch die zur Auswertung der Daten notwendigen Strukturinformationen müssen in 


--- Page 42 ---
 
Seite 42
maschinell auswertbarer Form zur Verfügung gestellt werden. 
Bei unvollständigen oder unzutreffenden Datenlieferungen kann die Finanzbehörde 
neue Datenträger mit vollständigen und zutreffenden Daten verlangen. Im Verlauf der 
Prüfung kann die Finanzbehörde auch weitere Datenträger mit aufzeichnungs- und 
aufbewahrungspflichtigen Unterlagen anfordern. 
Das Einlesen der Daten muss ohne Installation von Fremdsoftware auf den Rechnern 
der Finanzbehörde möglich sein. Eine Entschlüsselung der übergebenen Daten muss 
spätestens bei der Datenübernahme auf die Systeme der Finanzverwaltung erfolgen. 
177 
Der Grundsatz der Wirtschaftlichkeit rechtfertigt nicht den Einsatz einer Software, die 
den in diesem Schreiben niedergelegten Anforderungen zur Datenträgerüberlassung 
nicht oder nur teilweise genügt und damit den Datenzugriff einschränkt. Die zur Her-
stellung des Datenzugriffs erforderlichen Kosten muss der Steuerpflichtige genauso in 
Kauf nehmen wie alle anderen Aufwendungen, die die Art seines Betriebes mit sich 
bringt. 
178 
Ergänzende Informationen zur Datenträgerüberlassung stehen auf der Internet-Seite 
des Bundesministeriums der Finanzen zum Download bereit. Die Digitale Schnittstelle 
der Finanzverwaltung für Kassensysteme (DSFinV-K) steht auf der Internet-Seite des 
Bundeszentralamts für Steuern (www.bzst.de) zum Download bereit. 
12. Zertifizierung und Software-Testate 
179 
Die Vielzahl und unterschiedliche Ausgestaltung und Kombination der DV-Systeme 
für die Erfüllung außersteuerlicher oder steuerlicher Aufzeichnungs- und Aufbewah-
rungspflichten lassen keine allgemein gültigen Aussagen der Finanzbehörde zur 
Konformität der verwendeten oder geplanten Hard- und Software zu. Dies gilt umso 
mehr, als weitere Kriterien (z. B. Releasewechsel, Updates, die Vergabe von Zugriffs-
rechten oder Parametrisierungen, die Vollständigkeit und Richtigkeit der eingegebenen 
Daten) erheblichen Einfluss auf die Ordnungsmäßigkeit eines DV-Systems und damit 
auf Bücher und die sonst erforderlichen Aufzeichnungen haben können. 
180 
Positivtestate zur Ordnungsmäßigkeit der Buchführung - und damit zur Ordnungs-
mäßigkeit DV-gestützter Buchführungssysteme - werden weder im Rahmen einer 
steuerlichen Außenprüfung noch im Rahmen einer verbindlichen Auskunft erteilt. 
181 
„Zertifikate“ oder „Testate“ Dritter können bei der Auswahl eines Softwareproduktes 
dem Unternehmen als Entscheidungskriterium dienen, entfalten jedoch aus den in 
Rz. 179 genannten Gründen gegenüber der Finanzbehörde keine Bindungswirkung. 
13. Anwendungsregelung 
182 
Im Übrigen bleiben die Regelungen des BMF-Schreibens vom 1. Februar 1984 
(IV A 7 - S 0318-1/84, BStBl I S. 155) unberührt. 


--- Page 43 ---
 
Seite 43
183 
Dieses BMF-Schreiben tritt mit Wirkung vom 1. Januar 2020 an die Stelle des BMF-
Schreibens vom 14. November 2014 - IV A 4 - S 0316/13/10003 -, BStBl I S. 1450.  
184 
Die übrigen Grundsätze dieses Schreibens sind auf Besteuerungszeiträume 
anzuwenden, die nach dem 31. Dezember 2019 beginnen. Es wird nicht beanstandet, 
wenn der Steuerpflichtige diese Grundsätze auf Besteuerungszeiträume anwendet, die 
vor dem 1. Januar 2020 enden. 
 
 
 


--- Page 44 ---
 
Seite 44
Dieses Schreiben wird im Bundessteuerblatt Teil I veröffentlicht. 
 
Im Auftrag 
 
', '99188dfdc452af6c927dba5ff05f9c7a62eefb1bcf5134d205759c6925a29065', NULL, '2026-01-16T21:03:58.061188+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (4, 'website', 'https://teamdrive.com/en/knowledge/german-gobd/', 'GoBD - Principles for electronic accounting - TeamDrive', NULL, 'GoBD - Principles for electronic accounting
chatbot
Chatbot is loading...
Skip to content
Search
Blog
FAQ
WEB APP
LOGIN
LOGIN
+49
40 607709 300
Deutsch
English
+49
40 607709 300
Search for:
Full Site results
FAQ only results
Solution
TeamDrive
Professional
The secure cloud solution for
business customers
Show solution
TeamDrive
KMU/­Enterprise
Extensions for
large companies, authorities, ...
Show solution
Data exchange
Data exchange via the Internet (data room)
Send large files via link
Sending attachments via Outlook Add-in
Automatic data synchronization
Receive large files (Inbox)
Security
End-to-End Encryption
Secure sending of email attachments
Ransomware protection
Zero Knowledge Cloud
Secure mobile working (SecureOffice)
Documentation
Traceability through activity log
Monitoring retention periods
Immutable archiving
Location-independent work
Data access without VPN
Across companies
Multi-platform support
Data backup
Backup and data recovery
File versioning
Certified security
GDPR compliance
EuroPrise certificate
Free choice of server
Choice of cloud, hybrid or on-premise
Hosted cloud services in Germany (SaaS)
Azure Cloud / OneDrive can be selected as storage locations
Integrations
External authentication (Azure AD)
Integrate directory services (AD, Shibboleth, OAuth 2.0)
Integrations into third-party systems (API)
Workflow automation
Form integration (authorities/­healthcare/­orders)
Scan with Mobile Sandbox (Photos)
Connection of downstream systems
Individualization
Customization of external TeamDrive download pages
Customization of the inbox function
Individual email design
Security & Compliance
Monitoring retention periods
Unchangeable archives and logs (GoBD)
Immutable data storage (ransomware protection)
Users
TeamDrive can be used by
companies of all sizes
.
Companies
Self-employed people
Freelancers
One-man businesses
Small and medium-sized enterprises (SMEs)
Healthcare
Doctor''s offices
Pharmacies
Laboratories
Clinics
Hospitals
Research institutes
Finance and Legal Affairs
Tax consultants
Lawyers
Notaries
Auditors
Authorities / public administration
Universities
School administrations
Municipalities
District offices
State parliaments
Government organizations
Non-profit organizations
Societies
Associations
Daycare centers
Churches and parish offices
Pupils and students
Industry
Automobile manufacturing
Mechanical engineering
Transport/Vehicle driver
Journalism
Publishers
Investigative journalism
Whistleblower
Other
Works councils
Recruiter
Compliance
EuroPriSe
Certification history
TeamDrive Cloud certificates
The German GoBD
Health Care – HIPAA
The German Hospital Federation
TISAX
ITAR
CCPA
Knowledge
TeamDrive FAQ
Video tutorials
TeamDrive in comparison
Cloud Computing
Backup
ePrivacy, GDPR
E-Invoicing
The German GOBD
Encyrption
Ransomware
Security by Design
Shop
Downloads
TeamDrive App
TeamDrive Outlook Add-In
TeamDrive Server App
TeamDrive Personal Server
TeamDrive manuals
Remote Support
Solution
TeamDrive
Professional
Data exchange
Data exchange via the Internet (data room)
Send large files via link
Sending attachments via Outlook Add-in
Automatic data synchronization
Receive large files (Inbox)
Security
End-to-End Encryption
Secure sending of email attachments
Ransomware protection
Zero Knowledge Cloud
Secure mobile working (SecureOffice)
Documentation
Traceability through activity log
Monitoring retention periods
Immutable archiving
Location-independent work
Data access without VPN
Across companies
Multi-platform support
Data backup
Backup and data recovery
File versioning
Certified security
GDPR compliance
EuroPrise certificate
TeamDrive
KMU/Enterprise
Free choice of server
Choice of cloud, hybrid or on-premise
Hosted cloud services in Germany (SaaS)
Azure Cloud / OneDrive can be selected as storage locations
Integrations
External authentication (Azure AD)
Integrate directory services (AD, Shibboleth, OAuth 2.0)
Integrations into third-party systems (API)
Workflow Automation
Form integration (authorities/ healthcare/ orders)
Scan with Mobile Sandbox (Photos)
Connection of downstream systems
Individualization
Customization of external TeamDrive download pages
Customization of the inbox function
Individual email design
Security & Compliance
Monitoring retention periods
Unchangeable archives and logs (GoBD)
Immutable data storage (ransomware protection)
Users
Companies
Self-employed people
Freelancers
One-man businesses
Small and medium-sized enterprises (SMEs)
Healthcare
Doctor’s offices
Pharmacies
Laboratories
Clinics
Hospitals
Research institutes
Finance and Legal Affairs
Tax consultants
Lawyers
Notaries
Auditors
Authorities / public administration
Universities
School administrations
Municipalities
District offices
State parliaments
Government organizations
Non-profit organizations
Societies
Associations
Daycare centers
Churches and parish offices
Pupils and students
Industry
Automobile manufacturing
Mechanical engineering
Transport/Vehicle driver
Journalism
Publishers
Investigative journalism
Whistleblower
Other
Works councils
Recruiter
Compliance
EuroPriSe
Certification history
TeamDrive Cloud certificates
GDPR
Health Care – HIPAA
The German Hospital Federation
TISAX
ITAR
CCPA
Knowledge
TeamDrive FAQ
Video tutorials
TeamDrive in comparison
Cloud Computing
Backup
GDPR, ePrivacy
E-Invoicing
The German GOBD
Encyrption
Ransomware
Security by Design
Shop
Downloads
TeamDrive App
TeamDrive Outlook Add-In
TeamDrive Server App
TeamDrive Personal Server
TeamDrive manuals
Remote Support
Login
Web App
Search
Blog
FAQ
Contact
Data exchange.
Highly secure.
For self-employed people.
For small companies.
For large companies.
The TeamDrive cloud solution protects the data of
companies, authorities, organizations, law firms
and
associations
worldwide.
Get to know TeamDrive
from
6.33
€
*
Quickinfo
Tour
Video
GoBD – proper electronic accounting and archiving
Many companies already archive their documents and records electronically. However, there are requirements for the
digital storage
of relevant data which must be observed. One of these requirements is the principles for the proper management and storage of books, records and documents in electronic form and for data access (GoBD). We explain briefly and concisely the most important facts about the
proper accounting
and storage of electronic records.
Download Whitepaper for free
GoBD – what is that actually?
The
abbreviation GoBD
stands for the principles for the proper management and storage of books, records and documents in electronic form as well as for data access. It is an
administrative instruction
that was first issued by the Federal Ministry of Finance (BMF) in November 2014 and came into force during the same year. In November 2019, a BMF letter replaced the previous instruction. The ministry published a
new version
with numerous changes, which has been
valid since January 2020
. The decree of the tax authorities regulates the obligations for digital storage of tax-relevant data from accounting and business transactions.
To whom do the principles of the GoBD apply? They are
mandatory for all companies
. This is because the obligation regulated by law is not only binding for companies that have to present accounts, but also for small businesses, freelancers and the self-employed. If employees of the tax authorities find records and documents not recorded in
conformity with GoBD
during an operational audit, expensive consequences are imminent.
With the
introduction of the GoBD
, the previously obligatory principles of proper data processing supported accounting systems (GoBS) as well as the principles of data access and verifiability of digital documents (GDPdU) were summarized in an administrative instruction. In the old regulations it was regulated up to now that only enterprises are subject to the tax recording obligation if they also have to keep accounts. Only with the GoBD were other persons and companies included.
What needs to be done to fulfill the GoBD in the company?
The tax authorities demand audit-proof archiving from companies. Audit-proof procedural documentation or accounting means that all data subject to retention are excluded from
subsequent processing or manipulation
. This is particularly important if the documents are available in electronic form. The GoBD regulates these requirements precisely so that the accounting in the company remains traceable at all times for a tax consultant and the tax office.
Adaptation of the GoBD in 2020
In the new version of the GoBD, which has now been binding since January 2020, several
content has been supplemented
or made
more specific in the wording
. The reason for this was the rapid development of digital possibilities in recent years and the new electronic
solutions for bookkeeping
in companies that came along with it.
Therefore, among other things, it was reworded that
cloud systems
are now also suitable for the processing and storage of company documents and fulfill the requirements of IT-supported accounting systems. In addition, documents can now also be captured with the photo function of a smartphone and
stored in the cloud
. In addition, companies are no longer obliged to retain the
original paper documents
when filing electronically, as long as there is no change in content or important information is lost through conversion. Similarly, access for the tax authorities must not be restricted so that they can carry out their checks properly.
Six rules for tax-compliant accounting
1. Verifiability:
All postings in the company always follow the principle that
no posting is made without a receipt
. In addition, procedural documentation is required. This is because in the case of a tax audit, an external expert who is not involved in the internal control system must be able to obtain an overview of the business transactions and the situation of the company within a reasonable period of time.
2. Completeness:
According to the principle of the obligation to keep records of individual electronic invoices, every transaction in business operations must
be fully and completely
documented.
3. Timely and correct booking:
Another important point is the
timely recording
of business transactions. Financial transactions in cash must be recorded and booked within the same day. A period of ten days applies to cashless transactions. In addition to the time factor, the
correct documentation
of bookings also plays a major role. Only the actual circumstances in the business transactions may be represented.
4. Orderliness and immutability:
In the EDP
system bookings
are to be recorded systematically, so that by mechanical readability of the data also comprehensible results arise. The principles of clarity, unambiguousness and verifiability are applied.
Subsequent changes
must
be logged consistently
so that the original content can always be determined.
5. Security:
All electronic data must be protected against
unauthorized access
and also against
loss
.
6. Storage:
Electronically received documents and data are subject to a ten-year
retention period
. Business documents in the form of e-mails must be digitally archived for six to ten years. The form of the documents must be retained.
Obligation for procedural documentation
In addition to the principles of tax recording and retention of documents,
procedural documentation
must be established
. It helps to better check electronic accounting and describes the entire organizational and technical
process of archiving
. The following six steps belong to this process:
Creation (recording)
Indexing
Storage
Clear finding
Protection against loss and falsification
Reproduction of archived information
Retention periods of electronic documents
The
list
of electronic documents for which
retention periods
exist is long. The obligations to keep and not to change documents in IT-supported accounting include these proofs:
Accounting documents
Digital account books
Records of materials and merchandise management
Payroll accounting
Time Recording
Procedural documentation
These documents must be retained for
ten years
, while
different retention periods
apply for other documents, stacked records and business transactions according to the GoBD. The following list gives some examples:
commercial or business letters received
Reproduction of the commercial or business letters sent
other documents, insofar as they are relevant for taxation purposes
TeamDrive: GoBD-compliant software for document management
With the
TeamDrive software
, you can manage and archive your data and documents in an audit-proof manner. Our software enables companies to upload business
documents to the cloud
and store the data in an unalterable format. TeamDrive thus offers the possibility of
GoBD-compliant archiving
.
With each installation, TeamDrive Systems creates an RSA 2048/3072 key pair for confidential key exchange. All data is AES-256 encrypted before it is uploaded to the cloud. The keys remain with the user. With
end-to-end encryption
, only the user himself gains access to the unencrypted data.
The user creates a folder in which old documents can also be copied and backdated with the appropriate time of retention. A new version is saved with every change. An
indelible audit trail
guarantees the traceability of electronic archiving. Thus, our audit trail also replaces the manual process documentation.
For more detailed information, please request our GOBD
Whitepaper
.
Further knowledge on the subject area of the
German GOBD
GoBD
According to the
Principles of Proper Accounting
(GoBD)
, data and documents that are to be recognized by the tax authorities for
tax evidence
must be handled in a special way.
We will explain to you the most important facts about
archiving
and
storing electronic documents
.
Find out more about GoBD
Further knowledge in the areas of
data transfer
and
data storage
Cloud Computing
In the beginning,
cloud computing
was primarily understood to mean
the provision of storage volumes
via central data centers. Instead of buying storage, you could rent storage
flexibly and as needed
.
This continues to happen today in varying degrees, but the offering has been expanded to include numerous other interesting services from cloud providers.
Find out more about cloud computing
Backup
A backup is a backup copy of data that can be used to restore data if the original data is
damaged, deleted
or
encrypted
.
In the best case scenario, a backup should be stored in
a different location
than the original data itself -
ideally in a cloud
. You can find out why this is the case and what this has to do with
ransomware attacks
here.
Learn more about backup
GDPR, ePrivacy
With the introduction of the General Data Protection Regulation,
DSGVO
for short, extended requirements came into effect, especially with regard to
personal data protection
- including
sensitive sanctions
for violations of the law.
Read here what effects the GDPR has on you and your company.
The
ePrivacy Regulation
, which is still a work in progress at the moment, will also be discussed, but will in future formulate binding data protection rules that will apply
within the EU
.
Find out more about GDPR and ePrivacy
Encryption
In the digital age,
data protection
and
data security
play an outstanding role.
To ensure that electronic data cannot be viewed by third parties and to prevent data misuse, it must be encrypted. This applies both to their storage and, above all, to their transport via the public Internet.
You can get deeper insights into the topic of encryption here.
Learn more about encryption
Ransomware
Ransomware attacks
have increased significantly in recent years. After a successful attack, all data on your computer is
encrypted
. From this moment on you no longer have
any access
options. The economic damage to companies is often
enormous
.
Find out here how you can protect yourself against digital blackmail.
Learn more about ransomware
Security by Design
Especially with software that is intended to protect your users'' data from unauthorized access by third parties, software and data security must be taken into account and integrated into the
entire software life cycle.
You can find out why this is
very important
and how you as a user benefit from it here.
Find out more about Security by Design
About TeamDrive
Downloads
Partners & Resellers
Press
Vacancy
Whitepaper
Contact
FAQ
Support
Release Notes
Video tutorials
Imprint
Privacy Notice
GDPR Info for TeamDrive
Terms and conditions
Newsletter
Teamplace alternative
Boxcryptor alternative
Evaluation
© 2026 TeamDrive Systems GmbH
Page load link
Go to Top', '34e1a143756e6323eaf60c841a31740989a73e770087c0b950aeda57f4c39e73', '{"url": "https://teamdrive.com/en/knowledge/german-gobd/", "title": "GoBD - Principles for electronic accounting", "accessed_at": "2026-01-16T21:11:39.043053+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:11:39.044305+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (5, 'website', 'https://www.hornetsecurity.com/en/knowledge-base/what-is-gobd/', 'What is GoBD? | Knowledge Base - Hornetsecurity.com', NULL, 'What is GoBD? | Knowledge Base - Hornetsecurity.com
Skip to content
Skip to content
en
es
de
fr
ja
ca
us
Contact
24/7 Support
Partner Portal
Products
Products
HOLISTIC M365 SECURITY
365 Total Protection
All your M365 Security, Backup, GRC needs
Plan 4
Plan 3
Plan 2
Plan 1
Plan 1
SECURITY
Security Awareness Service
DMARC Manager
AI Cyber Assistant
Spam and Malware Protection
Advanced Threat Protection
Email Encryption
Email Archiving
Email Continuity Service
Email Signature and Disclaimer
Hornet.email
Hornet.email
GOVERNANCE, RISK & COMPLIANCE
365 Multi Tenant Manager for MSPs
365 Permission Manager
365 AI Recipient Validation
365 AI Recipient Validation
BACKUP
365 Total Backup
VM Backup
Physical Server Backup
Resources
Resources
BLOG
Hornetsecurity Blog
Security Lab Insights
Security Lab Insights
DIGITAL MEDIA
Webinars
Podcasts
Publications
Publications
MORE LINKS
Knowledge Base
Case Studies
Release Notes
Hornetsecurity Methodology
IT Pro Tuesday
IT Pro Tuesday
DOWNLOADS
VM Backup Downloads
Physical Server Backup Update
MSPs & Channel Partners
MSPs & Channel Partners
PARTNER
Partner Program
Partner Registration
Find a Partner
Find a Partner
DISTRIBUTORS
Find a Distributor
Find a Distributor
PARTNER PORTAL
Partner Portal Login
Company
Company
COMPANY
About us
International offices
Press Center
Awards
Analyst Relations
Case Studies
Case Studies
CAREER
Open Jobs
Benefits
Culture
Proactive Application
Employees wanted!
Employees wanted!
Events
Meet Hornetsecurity
Meet Hornetsecurity
PRIVACY
Legal notice
Privacy policy
Privacy Policy Business Contacts
Privacy Policy Services
Privacy Policy for applications
Code of Conduct
Partner Login
What is GoBD?
And what does GoBD mean for companies?
Home
»
Knowledge Base
»
GoBD
GoBD
are the Principles for properly maintaining, keeping and storing books, records and documents in electronic form and for data access, as provided by the German tax authorities. Put simply, the
GoBD
deals with how to store information electronically and how to handle tax-relevant documents. The documentation requirements, as well as the control and the use of appropriate IT are regulated in this context. The GoBD also regulates the access of auditors and the scope of the guidelines. Compliance with accounting processes and logging are also addressed.
Table of Contents
Who is affected by GoBD?
What relevance does GoBD have for individual companies?
What are the possibilities for companies with GoBD?
Who is affected by GoBD?
Generally, GoBD affects taxpayers with income from profit as well as all entrepreneurs who make their profit determination based on a revenue-surplus bill. In the event of an infringement of these requirements, appropriate fines may be set, and implementation of the specified measures could be ordered by the authorities.
It’s generally recommended to apply
email archiving
to all email communication, not only the portion that may be in question. This applies especially to any tax-relevant information. As of 01.01.2017, the new requirements have replaced the former GDPdU (principles for data access and verifiability of digital documents) and the GoBS (principles of lawful computer-aided accounting systems).
What relevance does GoBD have for individual companies?
The relevant change relates primarily to the recognition of digitally recorded documents. At first glance, they are basically on par with paper documents—so originals may be destroyed after digitization. However, a more refined approach is necessary because originals must still be submitted on request of the auditor.
The provision of digital documents explicitly requires that the storage fully conform to
GoBD
requirements. Simply storing emails on the hard disk is not enough. The hurdles are set significantly higher.
In addition, companies should consider that not all paper documents may be digitized and destroyed. From a legal point of view, the actual nature of a document also plays an important role. This applies, for example for notary contracts or authorizations.
1. Transparency
This refers to a complete list of all business transactions. An expert third party – usually a tax auditor – must be able to audit the transactions within a reasonable amount of time.
It includes all business transactions as well as the economic situation of the company. In addition, each business transaction must have a corresponding document. This ultimately makes documentation of the procedure necessary. As a result, the tax authorities have the opportunity to understand the complex processes in the document management system in detail.
This is due to the fact that electronic filing systems are constructed in different ways. This is the case with file extensions as well as the use of the appropriate filing system. For this reason, it is particularly important that transactions can be presented transparently to the auditor in the event of an audit.
2. Immutability
The criterion of immutability requires an identification of the changes made to tax-relevant data. The registration is thus absolutely necessary for the bookkeeping. This refers to whether the bookkeeping has taken place at regular intervals. If this is not the case, there is a formal deficiency in the bookkeeping system. Therefore, the commit time must be recorded in each case.
A booking record is considered unchangeable only through the final commit. Any control or authorization by other persons in the company remains unaffected, especially in the case of batch or preliminary entry.
The immutability is thus valid irrespective of whether it is an electronically supported record or a document in paper form. The records with document characteristics and the land registers (inward and outward registers) only have to be provided with a time. Furthermore, the auditor may request activity logs. This also applies to changes to the master data or in the software. For example, office formats often do not meet these requirements.
3. Neatness
According to this GoBD principle, it must be ensured that the systematic entry must be made in a clear format. It also has to be comprehensible with regard to the accounting entries. This meansthat within a certain period of time non-digitized accounting documents must be recorded by an orderly record.
This principle can be fulfilled by timely filing, which is continuously and clearly presented. However, it is important that the system properly documents the order and the access. An according systematic file folder fulfills the principle of neatness.
4. Completeness
Overall, there are a few deviations in retention periods. For balance sheets, contracts, invoices or inventory data, a storage period of 10 years applies. The storage requirements for commercial letters, costing or export documents are slightly lower at 6 years.
The retention period begins immediately after the end of the previous calendar year. In addition, different periods apply stating that the storage of audit-relevant documents must also take place in the case of an ongoing audit.
5. Timely bookings
According to the GoBD, cash transactions, such as income and expenses of corresponding cash accounts must be recorded daily. The same applies to corresponding land register records, which are regulated by software-based cash books. In this case, it is irrelevant what kind of POS system is used. EDP cash registers, loading and cash registers are thus equated.
In the case of non-cash business transactions, timely and consistent recording should also be carried out. The limits in the GoBD are defined in such a way that any non-operational deviation between the actual transaction and the entry itself is considered concerning. However, bookings which take place within up to 10 days, do not usually pose a problem.
Furthermore, the GoBD makes a distinction between goods and cost accounting. As a rule, accounting entries should not exceed a period of 8 days. Until then, the recorded business transactions are considered unobjectionable.
Ultimately, deviations based on an orderly and manageable document storage can be detected. The entries in the accounts may under certain circumstances not only be made until the end of the following month, but also be extended to one period.
6. Accuracy
The recording of business transactions must be in accordance with the actual circumstances in a company. The GoBD further demands compliance with legal requirements. Furthermore, archived documents always have to match the original.
What are the possibilities for companies with GoBD?
With the introduction of some of the latest GoBD innovations, companies have to decide whether to focus on digital document filing or continue paper-based archiving. The distinction between an original and a copy is not always immediately possible. For example, an invoice sent to you by post is an original. The same applies if an invoice arrives electronically in your mailbox. If you digitize the paper invoice by a scan, it can replace the physical paper original. Conversely, an invoice that was first in digital form and then printed on paper cannot be considered original. There is a very significant difference here.
1. The paperless office
Realizing a corporate environment that relinquishes all paper-based documents will be difficult, but not impossible in the future. The reason for this is the GoBD does not allow exclusive digital archiving for certain documents. For example, this applieds to tax or legal documents.
2. The double archiving
This might be the preferred solution for some companies, but it is time-consuming and inflexible. A double archiving effort is also extremely inefficient in terms of costs. Two side-by-side archiving systems, taken together, do not bring any significant advantages. In addition, not all information is available at any time and at any place, which is why this form of documentation is not recommended.
3. The solution: Legally compliant archiving through IT support from Hornetsecurity
A secure storage of sensitive content is of particular importance, especially when it pertains to emails. This is due to legal requirements as well as the significantly better discoverability of individual emails. Ultimately, this also allows selected third parties – in particular tax auditors – access to the relevant data over a certain period of time.
Another advantage that should not be underestimated is the simplicity of email management. Retention periods according to the GoBD can be set within a very short time by simply setting the archiving period. There is no additional administrative burdennor additional costs.
The email archiving by Aeternum of Hornetsecurity ensures legally compliant safekeeping. This applies in particular to the principle of immutability. Both inbound and outbound email traffic is duplicated on servers in an automated form by Hornetsecurity.
Learn about HORNETSECURITY’S SERVICES
Service
365 Total Protection
Full suite for Microsoft 365 Security, Risk, Governance, Compliance and Backup.
Read more
Service
Security Awareness Service
Fully automated, AI-powered Awareness Benchmarking, Spear-Phishing-Simulation and E-Training. Bring secure behavior to the next level.
Read more
Service
365 Total Backup
Automated Microsoft 365 data backup, recovery, and protection – any time and anywhere.
Read more
Load more
Interested in Related Topics?
Did you like our contribution to
GoBD
? Then other articles in our knowledge base might interest you as well! We help you learn more about cybersecurity related topics such as
Emotet
,
Trojans,
IT Security
,
Cryptolocker Ransomware
,
Phishing
,
GoBD
,
Cyber Kill Chain
and
Computer Worms
.
Visit our Knowledge Base
Holistic M365 Security
365 Total Protection
Security
Security Awareness Service
Spam and Malware Protection
Advanced Threat Protection
Email Encryption
Email Archiving
Email Continuity Service
Email Signature and Disclaimer
Governance, Risk & Compliance
365 Permission Manager
365 AI Recipient Validation
Backup
365 Total Backup
VM Backup
Physical Server Backup
Resources
Publications
Cloud Security Blog
Webinars
Podcasts
Security Lab Insights
Release Notes
Company
About Us
International
Career
Press Center
Awards
MSPs & Channel Partners
Partner Program
Partner Registration
Partner Portal
Legal
Privacy Policy
Legal notice
Privacy for applications
Privacy Policy for Services
Privacy Policy for Business Contacts
Proofpoint’s Position on the U.S. CLOUD Act
Code of Conduct and Code of Ethics
Regional Websites
United States
Italy
Canada (french)
CONTACT US!
SALES
+44 8000 246-906
24/7
SUPPORT
+44 2030 869-833
[email protected]
© 2026 Hornetsecurity GmbH. All rights reserved
24/7 Support
Partner Portal', 'af1511d43ce40090c85b057eef9fe9d5223d53a35f7ccf153d2b0a5c511bf4da', '{"url": "https://www.hornetsecurity.com/en/knowledge-base/what-is-gobd/", "title": "What is GoBD? | Knowledge Base - Hornetsecurity.com", "accessed_at": "2026-01-16T21:11:39.170468+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:11:39.171128+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (6, 'website', 'https://mxp-kb.mobilexpense.com/web-user-guide/working-version/what-is-gobd', 'What is GoBD? - MXP Help Center', NULL, 'What is GoBD? | MXP Help Center
Skip to main content
ð Commercial Website
Main navigation
Close navigation
Main
Mobile user guide
What is - mobile definitions
What is Spendcatcher?
What is an odometer?
What is OCR - mobile?
How to''s - mobile
How to log into the SpendCatcher app (v 4.4.0 and above)
How to download SpendCatcher
How to create an expense with OCR in SpendCatcher
How to create a mileage in SpendCatcher
How to split an expense with Spendcatcher
How to create an allowance in Spendcatcher
How to create an expense without OCR in SpendCatcher
How to create and submit a report in SpendCatcher
How to approve an expense report in SpendCatcher
How to create an Odometer in SpendCatcher
How to use the deputy function in SpendCatcher
How to manage receipts sent by email
How to send feedback through the app
How to export your private expenses from SpendCatcher
How to get help if you need a human by your side with SpendCatcher
FAQ - mobile
Web user guide
What is - definitions
What is an expense?
What is an allowance claim?
What is an allowance rate?
What is a mileage claim?
What is a mileage rate?
What is a travel claim?
What is an expense report?
What is an approval flow?
What is an expense category?
What is reimbursement?
What is a datafeed?
What is an accounting file?
What is VAT?
What is affidavit?
What is a cash advance?
What is SSO?
What is GoBD?
What is an ERP system?
What is an SFTP connection?
What is a digital signature?
What is sampling?
What is a settlement?
What is a deputy reporter?
What is a deputy approver?
What is an expense policy?
What is a cost center?
What is OCR?
What is cross-charge?
What is HCP?
What is SAF-T?
What is an API?
What is an integration?
How to''s
How to use the dashboard
How to create an expense
How to create an allowance claim
How to create an allowance claim - work in progress new module
How to create a mileage claim
How to split expenses
How to create a travel request
How to create an expense report
How to approve an expense report
How to approve a travel request
How to match my expenses with my credit card
How are SpendCatcher and card transactions merged?
How to upload an expense using OCR
How to add a deputy reporter/approver as an user
How to check who is the Supervisor/Controller
How to find your user information
How and why is an expense report sent back to Draft
How to mark privately-funded transactions as personal (PRIV-PRIV)
How to access the deputies list
FAQ
I received a password reset e-mail that I did not request
What to do if I cannot submit my transactions because they are showing as temporary?
How many types of Accounting file are there?
Insights user guide
What is - Insights definitions
What is Insights?
What is Tooltips?
What is an Insights Data Model?
What are the Date dimensions?
What is Drilling Into?
What is Drilling Down?
What is a dashboard?
How to''s - Insights
How to navigate the interface
How to use filtering
How to use of the Date Range filter
How to setup setting alerts
How to use drilling down
How to use drilling into
How to export dashboards and insights
How to scheduling emails
How to use Explore from here
How to create new and pre-built Dashboards
How to create new and pre-built insights
How to share dashboards
How to provide User Access and Login
How to grant access to the MXP Insights functionality
How to use Dashboards
A1. Expense/Spend analysis
A2. Spend patterns
A3. Merchant spend
A4. Private spend
C1. Controlling overview
C2. Controlling cycle times
C3. Report cycle overview
How to access the Report History data set
FAQ - Insights
ð Commercial Website
Main
What is - definitions
What is an expense?
What is an allowance claim?
What is an allowance rate?
What is a mileage claim?
What is a mileage rate?
What is a travel claim?
What is an expense report?
What is an approval flow?
What is an expense category?
What is reimbursement?
What is a datafeed?
What is an accounting file?
What is VAT?
What is affidavit?
What is a cash advance?
What is SSO?
What is GoBD?
What is an ERP system?
What is an SFTP connection?
What is a digital signature?
What is sampling?
What is a settlement?
What is a deputy reporter?
What is a deputy approver?
What is an expense policy?
What is a cost center?
What is OCR?
What is cross-charge?
What is HCP?
What is SAF-T?
What is an API?
What is an integration?
How to''s
How to use the dashboard
How to create an expense
How to create an allowance claim
How to create an allowance claim - work in progress new module
How to create a mileage claim
How to split expenses
How to create a travel request
How to create an expense report
How to approve an expense report
How to approve a travel request
How to match my expenses with my credit card
How are SpendCatcher and card transactions merged?
How to upload an expense using OCR
How to add a deputy reporter/approver as an user
How to check who is the Supervisor/Controller
How to find your user information
How and why is an expense report sent back to Draft
How to mark privately-funded transactions as personal (PRIV-PRIV)
How to access the deputies list
FAQ
I received a password reset e-mail that I did not request
What to do if I cannot submit my transactions because they are showing as temporary?
How many types of Accounting file are there?
Breadcrumbs
Web user guide
What is - definitions
On this Page
What is GoBD?
ð©ðª From a travel expense management perspective,
GoBD
(GrundsÃ¤tze zur ordnungsmÃ¤Ãigen FÃ¼hrung und Aufbewahrung von BÃ¼chern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff) refers to the German principles for the proper keeping and storage of books, records, and documents in electronic form, and for data access. These principles set the legal framework for how electronic records and financial data, including travel expense records, must be handled to ensure compliance with German tax laws.
ð Key Points:
Purpose
ð¡ : Ensures the proper management and storage of electronic financial records, including travel expenses, in compliance with German tax regulations.
Requirements
ð : Specifies guidelines for the accuracy, completeness, and retrievability of electronic records.
Documentation
âï¸ : Requires thorough documentation of processes and procedures for maintaining and accessing electronic records.
Retention
â³ : Mandates the retention of electronic records for a specified period (typically 10 years).
Auditability
ð§ : Ensures that electronic records are accessible and verifiable by tax authorities during audits.
Example:
A company using a travel expense management system in Germany must ensure that all electronic records of travel expenses are stored securely, can be retrieved accurately, and are kept for the required retention period, following GoBD guidelines.
Benefits:
Compliance
: Helps businesses adhere to German tax laws and avoid penalties.
Organization
: Ensures systematic and reliable record-keeping of travel expenses.
Audit Readiness
: Facilitates smooth and efficient audits by tax authorities.
If you want to learn more about GoBD and other compliance topics, please refer to the links below:
Compliance Center
Expense Report Compliance Made Global
Copyright
Powered by
Scroll Sites
&
Atlassian Confluence', '55317307d516308204ba101d73ff1c6bdb5e65d7b4db6763e6f71da85a9e360c', '{"url": "https://mxp-kb.mobilexpense.com/web-user-guide/working-version/what-is-gobd", "title": "What is GoBD? | MXP Help Center", "accessed_at": "2026-01-16T21:11:39.629726+00:00", "status_code": 200, "content_type": "text/html"}', '2026-01-16T21:11:39.630613+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (7, 'website', 'https://easy-software.com/en/glossary/gobd-principles-for-proper-bookkeeping/', 'GoBD – Principles for proper bookkeeping - Easy Software', NULL, 'The GoBD - What is it? Definition & explanation
Skip to content
easy portal
contact
×
language
Global (English)
Deutschland | Schweiz
Contact
Menu
Menu
Solutions
Application Areas
Know-how
Service & Support
Partner
About easy
language
Global (English)
Deutschland | Schweiz
Contact
×
Solutions
Powerful
ECM
solutions
Digital archiving, accounts payable, contracting, and HR management systems are available as on-premises, private cloud, cloud-native, or hybrid solutions. Intelligent workflows and AI services automate document-based processes.
easy
archive
Compliant archiving
easy
invoice
Secure invoice management
easy
DMS
Efficient document management
easy
contract
Transparent contract management
easy
hr
Smart HR management
SAP
Add-ons
Experience digitization easily and quickly with third-party systems, such as SAP
®
. easy provides SAP-certified add-ons.
learn more
Application Areas
references
Banks and insurances
Energy industry
Manufacturing and production
Public service and associations
View all industries
application areas
Archiving
Document Capture
Procurement Processes
Digital Personnel File
Digital Signature
Document Management
Incoming Invoices
Electronic File
Contract Management
Workflows
successful customers
Read success stories from some of the 5400+ easy customers who are digitally transforming their businesses with easy products.
case studies
Know-how
Knowledge
Browse the blog. Use free white papers, checklists and guidelines and expand your knowledge in webinars and at events.
Newsroom
Blog
Press & News
Glossary
Events & Whitepaper
Events & Live Webinars
Knowledge base
Guides & Checklists
Webinar Recordings
Whitepaper
Digital accounts payable processing
Find out in our white paper how your company can benefit from electronic incoming invoice processing.
free whitepaper
Service & Support
Resources
Develop your potential with the easy Academy, optimize your business with Managed Services and get help from professional easy Support.
Service
easy Academy
Managed Services
Support
Support
As a direct customer, you can benefit from our support.
support
Partner
Partner Network
Benefit from our experience as a software manufacturer for ECM, SAP, Cloud and SaaS solutions. We want to grow together with our software partners.
Partners
Become a partner
Find partners
easy Portal
Become a partner
Become part of our partner community and expand your business model with the successful easy product portfolio.
become a partner
About easy
easy group
With over 30 years of experience, easy software is one of the market leaders for archiving, ECM, and DMS software solutions in the German-speaking region.
About Easy
Company
Press & News
Customer Stories
Contact Us
If you have any questions, just let us know.
contact us
Solutions
easy
archive
Compliant archiving
easy
invoice
Secure invoice management
easy
contract
Transparent contract management
easy
hr
Smart HR management
easy
DMS
Efficient document management
SAP
Add-ons
Experience digitization easily and quickly with third-party systems, such as SAP
®
. easy provides SAP-certified add-ons.
learn more
Application Areas
references
Banks and insurances
Energy industry
Manufacturing and production
Public service and associations
View all industries
application areas
Archiving
Document Capture
Procurement Processes
Digital Personnel File
Digital Signature
Document Management
Incoming Invoices
Electronic File
Contract Management
Workflows
successful customers
Read success stories from some of the 5400+ easy customers who are digitally transforming their businesses with easy products.
case studies
know-how
Knowledge
Browse the blog. Use free white papers, checklists and guidelines and expand your knowledge in webinars and at events.
Newsroom
Blog
Press & News
Glossary
Events & Whitepaper
Events & Live Webinars
Knowledge base
Guides & Checklists
Webinar Recordings
Whitepaper
Digital accounts payable processing
Find out in our white paper how your company can benefit from electronic incoming invoice processing.
free whitepaper
Service & Support
Resources
Develop your potential with the easy Academy, optimize your business with Managed Services and get help from professional easy Support.
Service
easy Academy
Managed Services
Support
Support
As a direct customer, you can benefit from our support.
support
Partner
Partner Network
Benefit from our experience as a software manufacturer for ECM, SAP, Cloud and SaaS solutions. We want to grow together with our software partners.
Partners
Become a partner
Find partners
easy Portal
Become a partner
Become part of our partner community and expand your business model with the successful easy product portfolio.
become a partner
About easy
easy group
With over 30 years of experience, easy software is one of the market leaders for archiving, ECM, and DMS software solutions in the German-speaking region.
About Easy
Company
Press & News
Customer Stories
Contact Us
If you have any questions, just let us know.
contact us
easy portal
Glossary
GoBD
– Principles for proper bookkeeping
The principles for the proper keeping and storage of books, records and documents in electronic form and for data access are binding guidelines issued by the Federal Ministry of Finance (BMF) to tax authorities for digital bookkeeping and archiving.
As an
administrative regulation
, it governs how electronic business data must be recorded, processed, stored and retained in order to meet tax law requirements. The administrative regulation is primarily aimed at tax offices at federal, state or local level.
Requirements
for companies
arise
directly
from this regulation
.
Importance of the
GoBD
for companies
The GoBD ensure that tax-relevant data is archived in a tamper-proof and traceable manner. This includes in particular
Immutability
: Once recorded, data may not be changed without documentation.
Traceability
and
verifiability
: All postings must be comprehensible and fully traceable for audit authorities.
Regularity
: Data must be processed systematically and correctly.
Retention obligation
: Digital receipts and documents must be archived for up to ten years, depending on their type.
Data access
: The
tax authorities
have the right to direct or indirect access to tax-relevant data.
GoBD
and ECM systems
Enterprise content management
(ECM) systems support companies in efficiently implementing the GoBD requirements. Modern ECM solutions offer
Audit-proof archiving
of documents and receipts
Automated logging
of all changes and accesses
Access controls
to ensure data integrity
Digital workflows
for compliance with GoBD requirements
In many cases, an ECM system forms the basis for many other applications. It often includes
document management
to map document-intensive business processes, such as incoming invoice processes.
Conclusion
Compliance with the GoBD is essential for companies to ensure tax security and make digital processes legally compliant. An ECM system helps to meet the requirements efficiently and ensures secure, audit-compliant document management.
FAQ on
GoBD
Which documents are subject to the
GoBD
?
All tax-relevant documents fall under the principles. According to Section 147 (1) of the German Fiscal Code (AO), this includes books, records, management reports, annual financial statements, inventories, business and commercial letters and all types of
accounting vouchers
.
Are PDF documents subject to the
GoBD
?
Yes
, as soon as the PDFs are fiscally relevant. Incidentally, e-invoicing will be mandatory from January 2025 in Germany. As a result, invoices must be transmitted as a
ZUGFeRD PDF
or as an XRechnung and archived as an e-invoice in compliance with GoBD.
What are the consequences of
GoBD
violations?
As long as the bookkeeping is traceable and verifiable, violations of the GoBD do not necessarily lead to negative results.
If this is not the case
, there may be significant
additional tax estimates
.
To whom does the
GoBD
apply?
Die Grundsätze gelten für alle Unternehmen und Selbstständigen in Deutschland, die steuerlich relevante Daten verarbeiten. Dies umfasst insbesondere:
Companies
: All types of companies, regardless of their size or sector, must comply with the GoBD.
Self-employed persons
: Sole traders and freelancers are also obliged to comply with the principles.
Organizations
: Non-profit organizations and associations that process tax-relevant data must also comply with these principles.
easy
archive
Archive data securely and compliant.
discover easy archive
easy
invoice
Digitally verify and approve invoices.
discover easy invoice
Newsroom
Media Library
Glossary
Contact us
+1 267 313 57-80
info[at]easy-software.com
Newsletter
We will keep you regularly up to date. Subscribe to our newsletter and find out everything you need to know about the digitization of business processes. The topics will be prepared for you in a tailor-made and varied way.
Newsletter subscription
Solutions
easy
archive
easy
invoice
easy
contract
easy
hr
easy
DMS
Application areas
Archiving
Document Capture
Procurement Processes
Digital Personnel File
Digital Signature
Document Management
Incoming Invoices
Electronic File
Contract Management
Workflows
Resources
Support
Academy
Managed Services
Blog
Media Resources
Events
Customer Stories
Glossary
About easy
Company
Press & News
Investor Relations
Find partners
Application areas
Archiving
Document Capture
Procurement Processes
Digital Personnel File
Digital Signature
Document Management
Incoming invoices
Electronic File
Contract Management
Workflows
Resources
Support
Academy
Managed Services
Blog
Media Resources
Events
Customer Stories
Glossary
About easy
Company
Press & News
Investor Relations
Careers
Find partners
Social
easy
is a
conrizon
brand offering established software products for compliant archiving, invoice processing, contract management, and human resources management. The right solution for every challenge, industry and company size.
www.conrizon.com
Imprint
General terms and conditions
Disclaimer
Privacy
Privacy Settings
Search for:', '4502e6c53e7d65f39babe8c76e6c06acb41dc529abbd32c0c3769d3c216ab90a', '{"url": "https://easy-software.com/en/glossary/gobd-principles-for-proper-bookkeeping/", "title": "The GoBD - What is it? Definition & explanation", "accessed_at": "2026-01-16T21:11:40.122714+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:11:40.123382+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (8, 'website', 'https://www.comarch.com/trade-and-services/data-management/legal-regulation-changes/germany-updates-gobd-rules-to-reflect-mandatory-e-invoicing/', 'Germany Updates GoBD Rules to Reflect Mandatory E-Invoicing', NULL, 'Germany Updates GoBD Rules to Reflect Mandatory E-Invoicing
Products
BSS Products & Solutions
Comarch Business Banking
Comarch Custody
Comarch Digital Insurance
Comarch Insurance Claims
Comarch Factoring System
Comarch Life Insurance
Comarch Loan Origination
Comarch Wealth Management
Data Center & IT Services
e-Invoicing & EDI
E-learning
ERP-Schulungen
Free Resources
Intelligent Assurance & Analytics
IoT Connect
Loyalty Marketing Platform
OSS Products & Solutions
Training
Industries
Airlines & Travel
Finance, Banking & Insurance
Oil & Gas
Retail & Consumer Goods
Satellite Industry
Telecommunications
Utilities
Customers
Investors
About
About us
Awards
Comarch at a Glance
Comarch Group Companies
Corporate Social Responsibility
Management Board
Research & Development
Shareholders
Supervisory Board
Technology Partners & Industry Association
Contact
Contact a Consultant
Headquarters
Personal Data and Privacy Policy
Worldwide Offices
Press
News
Events
Comarch Telco Review blog
e-Invoicing Legal Updates
Loyalty Marketing Blog
Media Contact
Social media
Career
Partners
Language
EN
PL
EN
DE
FR
BE
NL
IT
ES
PT
JP
All categories
Other
Telecomunications
Finance
ERP
Large enterprises
Government
TV
Career
Investors
News and Events
Healthcare
Training
Field Service Management
Data Exchange
Customer Experience & Loyalty
ICT & Data Center
Language
EN
PL
EN
DE
FR
BE
NL
IT
ES
PT
JP
e-Invoicing & EDI
Solutions
AI-powered Data Management
e-Invoicing
Electronic Data Interchange
Global Compliance
Clients
Resources
Blog
Articles
Legal Updates
Vlogs: Digitalization Today
Events
Become a Partner
Contact us
Contact us
Comarch
Data Exchange & Document Management
Legal Regulation Changes
Get in touch!
Contact form
Partner Program
Newsletter
Germany Updates GoBD Rules to Reflect Mandatory E-Invoicing
Published
26 Aug 2025
Share
On July 14, 2025, the Federal Ministry of Finance (BMF) published the second amendment to the Principles for the Proper Management and Storage of Books, Records, and Documents in Electronic Form, and for Data Access (GoBD). The amendment, effective immediately, aligns the GoBD with Germany’s mandatory B2B e-invoicing regime, which entered into force on 1 January 2025.
Key Changes to the GoBD
Retention and Archiving Requirements
For e-invoices issued under Section 14(1) UStG, archiving the structured data component (e.g., XML file) is sufficient, provided all GoBD requirements are met.
A human-readable copy (such as a PDF) only needs to be stored if it contains additional tax-relevant information not included in the structured data.
Incoming electronic documents must be retained in the format received (e.g., invoices as XML, bank statements as PDF or image files).
For structured datasets, content conformity is required, but visual conformity is not.
Format conversions are allowed, but the original structured data must be preserved. If additional information is extracted (e.g., through OCR), the corrected data must also be retained.
Hybrid Invoices
For hybrid formats such as ZUGFeRD, the PDF component only needs to be archived if it contains different or additional tax-relevant details, such as accounting notes or electronic signatures.
Payment Processing Proofs
Technical proofs created by payment processors (e.g., logs) do not need to be stored unless they are used as accounting documents, are the only settlement record with the processor, or are the sole means of distinguishing cash from non-cash transactions.
Business Correspondence
Electronic business letters and accounting documents must be archived in the format in which they were received.
Audits and Data Access
Tax authorities may require a machine-readable evaluation of retained data or have it carried out by an authorized third party. Taxpayers must provide data in a processable export format or grant read-only access.
VAT-Related Changes
Alongside the GoBD update, Germany has also updated its VAT invoicing and archiving requirements to mandate B2B e-invoicing from January 1, 2025, in
structured XML formats
as the legal record, as well as reduced the statutory retention period for invoices
from 10 years to 8 years
. It also introduced
revised credit note
rules (effective 6 December 2024), under which a credit note with VAT issued by a non-entrepreneur may trigger unauthorized tax liability if not promptly objected to by the recipient.
There’s more you should know about
e-invoicing in Germany
–
learn more about the new and upcoming regulations.
Other news
08 Jan 2026
Sri Lanka Launches National E-Invoicing System to Modernize Tax Infrastructure
08 Jan 2026
Spain Approves Postponement of the Veri*factu Implementation
08 Jan 2026
Slovakia Approves Mandatory E-Invoicing and Reporting Framework
07 Jan 2026
Poland Finalizes Legal Framework for Mandatory KSeF 2.0
05 Jan 2026
Poland Publishes the Official List of KSeF E-Invoicing Exemptions
How Can We Help?
💬
Compliance issues? Supply chain trouble? Integration challenges? Let’s chat.
Schedule a discovery call
Newsletter
Expert Insights on
Data Exchange
We always check our sources – so, no spam from us.
Sign up to start receiving:
legal news
expert materials
event invitations
Please wait
Data Exchange & Document Management
AI-powered Data Management
e-Invoicing
Global Compliance
E-Invoicing in Poland (KSeF)
Electronic Data Interchange (EDI)
About
Data Management News
Legal Regulation Changes
Data Management Events
Vlogs - Digitalization Today
What''s openPEPPOL?
Resources
Client & Success Stories
Glossary
Other Products & Services
Loyalty Marketing Platform
Information and Communication Technologies
Contact
info@comarch.com
Contact form
Partner Program
Newsletter
Follow us on
linkedin
Follow us on
youtube
Comarch Group
Home
About the Comarch Group
Other Industries
Comarch Group Customers
Investors
Copyright © 2015 - 2026 Comarch SA. All rights reserved.
Personal Data and Privacy Policy
|
Cookie settings
Q6HKO63D8VIE0IKVC5J3', '577906de8227791b044db686e46b39fc465a59c0d5558e5e5e490eced8edbbb3', '{"url": "https://www.comarch.com/trade-and-services/data-management/legal-regulation-changes/germany-updates-gobd-rules-to-reflect-mandatory-e-invoicing/", "title": "Germany Updates GoBD Rules to Reflect Mandatory E-Invoicing", "accessed_at": "2026-01-16T21:11:40.650708+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:11:40.651418+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (9, 'website', 'https://www.flick.network/en-de/gobd-electronic-archiving-requirements', 'Electronic Archiving Requirements under GoBD in Germany', NULL, 'GoBD Electronic Archiving Requirements | Germany Compliance Guide
Products
E-Invoicing Softwares & Other Offerings
E-Invoicing in UAE
Get Started with FTA E-Billing Regulations
E-Invoicing in Saudi Arabia
Get complied for ZATCA E-Fatoora Mandates
Treasury Management Suite
Be confident with Organizational liquidity
VeriFactu Solution in Spain
Get covered for Fiskalisation in Spain
E-Invoicing Solution in Belgium
Get complied for Belgium
E-Invoicing Solution in Poland
Get complied for Poland
E-Invoicing in Malaysia
Integrate with IRBM/LHDN MyInvois Portal
E-Invoicing in Singapore
Get complied for Singapore
Global e-Invoicing
Get complied for e-Invoicing Mandates globally
Resources
Learning Resources
Our Blog
Read all updates happening around the globe
Announcements
Get notified With all new announcements that just released
Case studies
Learn how our customers solved their business struggles
Demo Videos
Watch how our system works in real-world scenarios
Our Webinars
Request On-Demand Webinars from our Experts
About Company
Learn about Flick and the vision we have for your future
featured Products
E-Invoicing in Saudi Arabia
Get complied for ZATCA E-Fatoora Mandates
E-Invoicing in UAE
Get Started with FTA E-Billing Regulations
E-Invoicing in Malaysia
Integrate with IRBM/LHDN MyInvois Portal
E-Invoicing in Singapore
Get Started with InvoiceNOW requirements
VeriFactu Solution in Spain
Get covered for Fiskalisation in Spain
E-Invoicing Solution in Belgium
Get covered for Peppol in Belgium
E-Invoicing Solution in Poland
Get covered for Peppol in Poland
Integrations
Customers
Partners
Support Desk
Contact Us
Our Products:
Recently Published
Current Status of B2B E-Invoicing in Germany (2025 Update)
Mandatory E-Invoice Reception in Germany – January 2025
Germany’s Phased E-Invoicing Timeline (2025–2028)
Allowed Invoice Formats in France & Germany (2025–2028)
BMF Clarifications on Germany’s E-Invoicing Mandate (June 2025 Draft)
Electronic Archiving Requirements under GoBD in Germany
E-Invoicing in Germany – Requirements, Deadlines, and Compliance Guide
Corporate Tax in Germany – Rates, Compliance, and Filing Guide (2025)
Personal Income Tax in Germany 2025: Rates, Deductions, and Filing Deadlines
VAT in Germany 2025: Rules, Registration, and Compliance Guide
Home
/
•
Germany e-Invoicing
/
•
Electronic Archiving Requirements Under Gobd In Germany
Electronic Archiving Requirements under GoBD in Germany
F
Flick team
•
Last updated at
December 10, 2025
Book a Demo
Learn more about this by booking a demo call with us. Our team will guide you through the process and answer any questions you may have.
Book Now
Electronic archiving requirements (GoBD)
With the digitalization of businesses today, the shift from paper records to electronic systems has significantly enhanced the operations'' efficiency. It has also brought new issues in the sense of data security, authenticity, and access. Electronic data is inherently more vulnerable to tampering with data, unauthorized use, and theft compared to physical documents and therefore poses a serious compliance and security risk to organizations.
To address such challenges, regulatory bodies and governments of the world have developed effective frameworks of electronic data management. In Germany, the "Principles for the Proper Management and Storage of Books, Records, and Documents in Electronic Form and for Data Access" (GoBD) is a standardized approach to retaining the integrity, authenticity, and accessibility of the records in electronic form.
This guide outlines the essential prerequisites of the GoBD according to electronic archiving, its relevance, as well as how companies can implement compliant archiving systems for ensuring operational and legal security.
Understanding GoBD Requirements
The GoBD aims to standardize electronic recordkeeping through three core objectives:
Integrity – All records so that they are complete and accurate.
Authenticity – Making sure that documents are original and tamper-free.
Accessibility – Offering secure but reliable access to the records at the required time.
To meet these aims, requirements of GoBD harmonize with tax legislation, data security laws, and accounting principles. The guiding rules are:
Completeness and Consistency: Each business transaction has to be completely recorded to provide dependable records.
Auditability and Verifiability: The records should enable the auditors and tax administrations to reconstruct and verify business procedures in full.
Data Access and Documentation: Companies have to be in a position to produce requested documents on time in an auditor-readable format.
GoBD requirements apply to different document types, such as accounting records, invoices, contracts, and corresponding business communication.
Importance of Archiving for GoBD Compliance
Archiving is not just a compliance requirement—it''s an essential business and risk management process:
Data Integrity and Authenticity Maintenance: Documents stored must be tamper-evident with the same original metadata and content.
Simplification of Inspections and Audits: Organized archives enable auditing by simple access to documents at the location.
Legal Compliance: Authentic evidence, well-maintained documents neutralize the risk of fines or lawsuits.
Business Risk Reduction: Effective archiving protects organizations from illegal use, data loss, and business disruption.-
Increased Business Efficiency: Systemized records strengthen the exchange of information, and that leads to more efficient decision-making along with business reactivity.-
GoBD Archiving Requirements
In order to stay compliant, companies need to ensure that their computer files meet the following GoBD requirements:
Durability and Retention: Data should be stored in a stable form for the legislatively prescribed retention period without loss of data or metadata.
Unalterability and Auditability: Files submitted should be tamper-proof, and changes should be traced by an audit trail.
Accessibility and Retrieval: Companies should be able to retrieve documents along with their related metadata at the time of request.
Format and Structure: Archives must maintain original document readability and structure regardless of formats or software systems in the future that will experience changes.
Security and Confidentiality: Archives should be shielded from unauthorized access and loss of information by access control, encryption, and authentication.
Regular Auditing and Review: Procedures for archiving should be regularly reviewed to find gaps in compliance and maintain constant compliance with GoBD requirements.
Implementing GoBD-Compliant Archiving Systems
To become completely compliant, corporations should follow a methodical process:
Assess Current Practices: Identify where existing recordkeeping processes are lacking and where they are non-compliant.)
Define Archiving Policies: Create clear policies for document classification, retention, access, and auditing.)
Choose Archiving Solutions: Choose those systems that can provide for unalterability, secure storage, and scalability.
Implement Technical Controls: Impose encryption, access control, and metadata tagging in order to maintain compliance.
Train Employees: Educate personnel on archiving rules and the relevance of compliance with GoBD obligations.
Document Policies and Systems: Keep records in full for policies, settings, and audit trails to prove compliance./
Monitor and Audit Regularly: Conduct internal audits and quality checks to verify ongoing compliance with regulations.-
Conclusion
Compliance with GoBD, is not merely a regulatory requirement, but also a commercial imperative for the modern digital economy. Organizations are able to protect their data, facilitate audit ease, reduce business risk, and enhance operating efficiency through the use of secure electronic archiving systems.
Pre-emptive compliance with GoBD bookkeeping standards improves business resiliency, protects against fines from regulatory bodies, and improves stakeholder trust. For German-based companies as well as those trading with the German market, electronic compliant book-keeping is required for sustainable growth and achievement.
Quick Navigation
Book a Demo
Learn more by booking a demo with our team. We''ll guide you step by step.
Book Now
Flick Network is a leading provider of innovative financial technology solutions, specializing in global e-invoicing compliances, PEPPOL & DBNAlliance integrations, AP/AR automations, Treasury & Cash Management.
Solutions
E-Invoicing in Malaysia
E-Invoicing in Saudi Arabia
E-Invoicing in UAE
E-Invoicing in Singapore
Treasury Management
General
Home Page
About Company
Contact us
Blog Updates
Sitemap
Resources
Developer Portal
System Status
Documentations
Raise a Ticket
Integration Videos
Flick Network ©️
2026
Privacy Policy
Terms and Conditions', '6557a70566d754e81ea537b000b2662f8f6c45f25bc00474a6a2baeefaa41708', '{"url": "https://www.flick.network/en-de/gobd-electronic-archiving-requirements", "title": "GoBD Electronic Archiving Requirements | Germany Compliance Guide", "accessed_at": "2026-01-16T21:12:45.859717+00:00", "status_code": 200, "content_type": "text/html; charset=utf-8"}', '2026-01-16T21:12:45.860399+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (10, 'website', 'https://www.aodocs.com/blog/gobd-explained-requirements-for-audit-ready-digital-bookkeeping-in-germany-and-beyond/', 'GoBD Explained: Requirements for Audit-Ready Digital ... - AODocs', NULL, 'GoBD Explained: Requirements for Audit-Ready Digital Bookkeeping in Germany and Beyond - AODocs
Skip to content
Products
Product
Document control
AI Process Automation
AI Assistant
Policies & Procedures
Legacy Replacement
Quality Management
Content Assembly
Record Management and Retention
Enterprise Apps
Google Workspace
Microsoft 365
SAP
View all integrations
Solutions
Industry
Manufacturing
Engineering and Construction
Energy
Healthcare
Professional Services
Banking and Insurance
Life Sciences
Line of Business
Human Resource Management
Legal
Finance & Procurement
Resources
Blog
Videos
Insights
Newsletter
Success Stories
Support
Migration Services
Implementation and Deployment Services
Knowledge Base
Status Page
Support Community
API Documentation
Company
About us
Careers
Contact
Contact us
Log in
Products
Product
Document control
AI Process Automation
AI Assistant
Policies & Procedures
Legacy Replacement
Quality Management
Content Assembly
Record Management and Retention
Enterprise Apps
Google Workspace
Microsoft 365
SAP
View all integrations
Solutions
Industry
Manufacturing
Engineering and Construction
Energy
Healthcare
Professional Services
Banking and Insurance
Life Sciences
Line of Business
Human Resource Management
Legal
Finance & Procurement
Resources
Blog
Videos
Insights
Newsletter
Success Stories
Support
Migration Services
Implementation and Deployment Services
Knowledge Base
Status Page
Support Community
API Documentation
Company
About us
Careers
Contact
GoBD Explained: Requirements for Audit-Ready Digital Bookkeeping in Germany and Beyond
August 15, 2025
Home
»
Blog
»
GoBD Explained: Requirements for Audit-Ready Digital Bookkeeping in Germany and Beyond
With Germany’s 2025 GoBD update tied to the e-invoicing mandate, businesses must now archive invoices in both human-readable and original XML formats. The shift reinforces GoBD’s core principles of immutability and audit-readiness—standards that may influence compliance thinking across Europe, much like SEC and FINRA rules shape recordkeeping in the U.S.
For companies operating in Germany—or with German business ties—the
GoBD
framework remains a key requirement: it governs how tax-relevant data must be recorded, retained, and protected. While not a law per se, GoBD carries legal weight—failure to comply can result in fines or audit complications.
The 2025 amendments
, introduced alongside Germany’s
e-invoicing mandate
, bring new clarity to how e-invoices must be stored. Businesses are now required to ensure that electronic invoices are archived in their original, machine-readable XML format, alongside any human-readable versions, and that the archived data meets the accessibility, and audit-readiness standards
defined in the GoBD
. Just as
SEC and FINRA
rules demand rigorous, audit-ready records for U.S. financial institutions, GoBD emphasizes
immutability, traceability, and structured recordkeeping
as core compliance principles.
Understanding GoBD
The
GoBD
(
Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff
) is an
administrative regulation from Germany’s Federal Ministry of Finance
. It clarifies obligations under Germany’s Fiscal Code (§§ 146 and 147 AO) concerning digital bookkeeping, record retention, and data access.
Key principles include:
Traceability and verifiability
of all entries and records
Immutability
— once data is recorded, it must remain unchanged
Timely and systematic recording
of business transactions
10-year retention
of tax-relevant digital documents
Documentation of IT systems and processes
Ensuring auditor access
to records when required
The 2025 update places special emphasis on
e-invoice archiving
: companies must store invoices in both human-readable and original structured data formats, maintain them in a compliant archive system, and ensure they can be accessed by auditors without alteration. GoBD applies to both digital and paper documents—especially when they are created, received, or stored electronically—and covers a comprehensive range of tax-relevant records, not just receipts.
Common Pitfalls in GoBD Compliance
Organizations often stumble when implementing GoBD, and the 2025 update introduces new challenges and risks tied to the e-invoicing mandate. Common pitfalls now include:
Incorrect e-invoice archiving
— storing only the human-readable PDF or print version, but not the original structured XML data required under the amended rules.
Storing records in formats that can’t be reproduced
exactly, compromising authenticity.
Failing to maintain version history or audit logs
, hindering traceability.
Inadequate enforcement of retention periods
or inconsistent deletion practices.
Scattered systems
with no centralized oversight or governance for audit readiness.
The e-invoicing changes mean companies can no longer treat invoice retention as a “PDF filing” exercise — both the
machine-readable source data
and any
human-friendly formats
must be preserved in compliance with immutability, accessibility, and auditability requirements. Businesses that overlook this are likely to fail an audit, even if other bookkeeping processes are sound.
Such issues can lead to audit challenges—or worse, penalties for noncompliance.
Building a Strong GoBD Compliance Framework
Compliance with GoBD—and safeguarding against audit complications—requires a methodical approach:
Immutable storage
to preserve original content and deter tampering
Centralized archives
to ensure consistency and governance across teams
Detailed audit trails
, capturing every access, change, and transfer
Automated retention policies
, aligned with statutory schedules
Secure, role-based access controls
, balancing security with usability
These steps help ensure records remain reliable, accessible, and defensible over time.
Why GoBD Is Worth Knowing Beyond Germany
GoBD often inspires similar thinking about data integrity across Europe, although it doesn’t carry direct force outside Germany. While it serves more as an example than a template, it can inform recordkeeping rigor expected in other jurisdictions. Being aware of these requirements—especially new rules on digital invoice archiving—is essential for companies that need to meet regulatory bookkeeping obligations across various territories and regulatory frameworks.
GoBD offers a concrete example of how strict digital recordkeeping principles may be applied in a regulatory context—especially regarding
immutability
,
auditability
, and long-term retention. Companies with operations in or connections to Germany must comply, and others may find it informative when evaluating or designing their own compliance frameworks.
While
some EU member states impose retention periods of
5–10 years
for tax-related records
, the details vary significantly by country. In that regard, while not a standard adopted elsewhere, GoBD can serve as a reference point.
How AODocs Compliance Archive Supports GoBD—and Similar Needs
Drawing on the same strengths that help
U.S. financial institutions comply with SEC and FINRA requirements
, AODocs’
Compliance Archive
is an effective solution for GoBD compliance:
Built-in
immutability features
, version control, and audit logging
Automated retention schedules
customized for German (and potentially other) regulatory periods
Centralized compliance dashboards
granting oversight across offices and functions
Seamless
integration with Google Workspace
, preserving workflows while enforcing compliance
AODocs helps organizations meet GoBD’s demands today, while staying adaptable for any evolving compliance standards across Europe or beyond.
Learn More:
How AODocs ensures FINRA compliance for U.S. financial institutions
Explore AODocs Compliance Archive solutions
Understanding how Google Workspace enables secure, compliant operations
SHARE:
Read next:
Blog
,
Compliance
,
Integrations
,
News & Announcements
,
SAP
AODocs Document Management Certified for SAP® Cloud ERP
AODocs’ AI-powered, cloud-native platform integrates with SAP to modernize document management, enhance security, and simplify compliance across ERP workflows. AODocs,...
December 16, 2025
Blog
,
AI
,
DMS
,
Knowledge Management
,
News & Announcements
AODocs Recognized in Gartner® Innovation Guide for Generative AI Knowledge...
AODocs announces it has been recognized as an Emerging Specialist in the Gartner® Innovation Guide for Generative AI Knowledge Management...
December 2, 2025
Blog
Beyond All-or-Nothing: Why Enterprise AI Agents Must Augment, Not Replace,...
The conversation around the rise of AI agents in work is often framed in black-and-white terms. An argument may sound...
December 2, 2025
Ready to get started?
See what AODocs can do for your company, let''s connect
Request a demo
Contact us
Linkedin
Youtube
Recognized by G2
Products
Document control
AI Assistant
Policies & Procedures
Legacy Replacement
Quality management
Content Assembly
Record Management and Retention
Google Workspace
Microsoft 365
SAP
Integrations
Document control
AI Assistant
Policies & Procedures
Legacy Replacement
Quality management
Content Assembly
Record Management and Retention
Google Workspace
Microsoft 365
SAP
Integrations
Solutions
Manufacturing
Engineering and Construction
Energy
Healthcare
Professional Services
Banking and Insurance
Life Sciences
Human Resource Management
Legal
Finance & Procurement
Manufacturing
Engineering and Construction
Energy
Healthcare
Professional Services
Banking and Insurance
Life Sciences
Human Resource Management
Legal
Finance & Procurement
Support
Migration Services
Implementation & Deployment Services
Knowledge base
Status page
Support community
API Documentation
Migration Services
Implementation & Deployment Services
Knowledge base
Status page
Support community
API Documentation
Resources
Blog
Videos
Insights
Newsletter
Success Stories
Blog
Videos
Insights
Newsletter
Success Stories
Company
About us
Careers
Contact us
About us
Careers
Contact us
Legal
Terms of Service
Professional Services Terms
Privacy Policy
Data Processing Agreement
Cookie Policy
Impressum
Terms of Service
Professional Services Terms
Privacy Policy
Data Processing Agreement
Cookie Policy
Impressum
Google disclosure
U.S. Patent 10,635,641
U.S. Patent 9,817,988
Copyright © 2012-2025 Altirnao Inc. All rights reserved.
We are using cookies to give you the best experience on our website.
You can find out more about which cookies we are using or switch them off in
settings
.
Accept
Close GDPR Cookie Settings
Privacy Overview
Strictly Necessary Cookies
Analytics
Powered by
GDPR Cookie Compliance
Privacy Overview
This website uses cookies so that we can provide you with the best user experience possible. Cookie information is stored in your browser and performs functions such as recognising you when you return to our website and helping our team to understand which sections of the website you find most interesting and useful.
Strictly Necessary Cookies
Strictly Necessary Cookie should be enabled at all times so that we can save your preferences for cookie settings.
Enable or Disable Cookies
Enabled
Disabled
Analytics
This website uses Google Analytics to collect anonymous information such as the number of visitors to the site, and the most popular pages.
Keeping this cookie enabled helps us to improve our website.
Enable or Disable Cookies
Enabled
Disabled
Enable All
Save Settings', 'ee5bfbb3e8111622bb132428f586c814cd176ca7c388510862be65d07aab183b', '{"url": "https://www.aodocs.com/blog/gobd-explained-requirements-for-audit-ready-digital-bookkeeping-in-germany-and-beyond/", "title": "GoBD Explained: Requirements for Audit-Ready Digital Bookkeeping in Germany and Beyond - AODocs", "accessed_at": "2026-01-16T21:12:46.541039+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:12:46.541785+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (11, 'website', 'https://learn.microsoft.com/da-dk/dynamics365/business-central/localfunctionality/germany/process-for-digital-audits', 'Digital audits (GoBD/GDPdU) - Business Central - Microsoft Learn', NULL, 'Digital audits (GoBD/GDPdU) - Business Central | Microsoft Learn
Spring til hovedindhold
Spring til Ask Learn-chatoplevelsen
Denne browser understÃ¸ttes ikke lÃ¦ngere.
Opgrader til Microsoft Edge for at drage fordel af de nyeste funktioner, sikkerhedsopdateringer og teknisk support.
Download Microsoft Edge
Flere oplysninger om Internet Explorer og Microsoft Edge
Indholdsfortegnelse
Afslut redigeringstilstand
SpÃ¸rg Learn
SpÃ¸rg Learn
Fokustilstand
Indholdsfortegnelse
LÃ¦s pÃ¥ engelsk
TilfÃ¸j
FÃ¸j til plan
Rediger
Del via
Facebook
x.com
LinkedIn
Mail
Udskriv
BemÃ¦rk
Adgang til denne side krÃ¦ver godkendelse. Du kan prÃ¸ve at
logge pÃ¥
eller
Ã¦ndre mapper
.
Adgang til denne side krÃ¦ver godkendelse. Du kan prÃ¸ve at
Ã¦ndre mapper
.
Process for digital audit (GoBD/GDPdU)
Feedback
Sammenfat denne artikel for mig
I denne artikel
You can export data from Business Central according to the process for digital audit (GoBD/GDPdU), which is based on German tax law.
Overview
Section 146 and 147 of the German Fiscal Code (Abgabenordnung, AO) allows tax authorities to assess the data of electronic accounting systems digitally. They may do this with a data storage device submitted to them or by direct or indirect access to the system. In the data storage device scenario, the tax liable company (or the person or entity entrusted with accounting and tax duties) must provide appropriate data storage devices with the data in computer-readable form. This means for the tax authorities that they''ll be able to access at will all stored data, including the master data and connections with sort and filter functions. To provide data that can be used and evaluated in this manner, you must define and standardize the file formats for submission by data storage device.
Tax authorities in Germany use analysis software, IDEA, which imports data from ASCII files. The IDEA software can import data in variable length or fixed length format. It requires an XML file,
index.xml
, that describes the structure of the data files.
Defining GDPdU export data
You can configure Business Central to export data to meet your needs. You can export large sets of data, and you can export small sets of data. You can export data from a single table or a table and related tables.
For each data export, you define the tables and fields that you want to export. This depends on the auditor''s requests. The selected information is exported to the ASCII files. A corresponding XML file,
INDEX.XML
, is also created to describe the ASCII file structure.
The elements in the
INDEX.XML
file define the names of the tables and fields that are exported. Because the current auditing tool has restrictions on these field names, such as the length and the characters that are used, Business Central removes spaces and special characters and then truncates the names to match the 20 character limitation. You can change the suggested table and field names when you add fields to a table definition.
In most cases, you set up GDPdU data export one time, and then a person in your company can run the export when the auditor requests new data. It''s recommended that the setup is handled by people with an understanding of the database structure and the technical hardware in your company, but also in collaboration with people who understand the business data, such as the accountant.
Configuration
You can set up different GDPdU data exports depending on the type of data that you want to be able to export. For example, you can create two GDPdU data exports:
One exports high-level information about all general ledger entries, customer ledger entries, vendor ledger entries, and VAT entries.
The other exports detailed information about the general ledger entries.
Note
How to set up the GDPdU data exports depends on your companyâs needs and the auditorâs requests.
Walkthrough: Exporting Data for a Digital Audit
provides an example of how to set up data exports for GDPdU.
Data export filters
When you set up a data export, you can filter data on different levels as described in the following table.
Filter level
Description
Period filters
You can specify a start date and end date for the data that is exported. You can then use this period filter to filter the data. For example, if you set a period filter for the export, you can then set table filters that use the period.
Table filters
You can set filters on each table in the export. For example, you can include only open ledger entries, or entries that have a posting date in the specified filter. For example, you can also set a filter that is based on FlowFields, such as
Net Change (LCY)
, to only export customers where there''s a change.
Important:
You can''t set a table filter that is based on a FlowFilter.
When you add table filters, you can increase performance by specifying the fields that the exported data will be sorted by the value of the
Key No.
field for the record definition. Which keys to use depends on the table. For example, if the table only has two key fields and relatively few entries, then the sort order doesn''t affect the speed of the export. But for a table such as G/L Entry, the export is faster if you specify the key in advance, such as the
G/L Account No.,Posting Date
key. If you don''t specify a key, then the primary key is used, which might not be the best choice.
Other tables in which it can be useful to specify the key include the
Cust. Ledger Entry
and
Vendor Ledger Entry
tables.
FlowField filters
You can include FlowFields in the export and set filters based on the period. For example, you can apply the period filter to the
Balance at Date
field on the
G/L Account
table.
If you include a FlowField such as the
Net Change (LCY)
field on the
Customer
table, you can specify that the entries must be filtered based on the remaining amount at the end date of the GDPdU period. If you add this as a field filter, then the calculation formulas are based on the dates that are specified during the export.
Learn more in
GDPdU Filter Examples
.
Export performance
If you want to export large sets of data, it can take a long time. We recommend that you set up data exports based on advice from your tax advisor to establish your business needs, and the requirements of the tax auditor. The number of records in a table is also something that you should consider.
Related information
Set Up Data Exports for Digital Audits
Export Data for a Digital Audit
Walkthrough: Exporting Data for a Digital Audit
Germany Local Functionality
Find free e-learning modules for Business Central here
Feedback
Var denne side nyttig?
Yes
No
No
Har du brug for hjÃ¦lp til dette emne?
Vil du prÃ¸ve at bruge Ask Learn til at tydeliggÃ¸re eller guide dig gennem dette emne?
SpÃ¸rg Learn
SpÃ¸rg Learn
Vil du foreslÃ¥ en rettelse?
Yderligere ressourcer
Last updated on
2024-06-11
I denne artikel
Var denne side nyttig?
Yes
No
No
Har du brug for hjÃ¦lp til dette emne?
Vil du prÃ¸ve at bruge Ask Learn til at tydeliggÃ¸re eller guide dig gennem dette emne?
SpÃ¸rg Learn
SpÃ¸rg Learn
Vil du foreslÃ¥ en rettelse?
da-dk
Dine valg af beskyttelse af personlige oplysninger
Tema
Lys
MÃ¸rk
HÃ¸j kontrast
AI-ansvarsfraskrivelse
Tidligere versioner
Blog
Bidrag
Beskyttelse af personlige oplysninger
VilkÃ¥r for anvendelse
VaremÃ¦rker
© Microsoft 2026', '88e7a1a030bc1b689a8e093db12f93484aa17d3fa989f266993ddf38a5ed68f7', '{"url": "https://learn.microsoft.com/da-dk/dynamics365/business-central/localfunctionality/germany/process-for-digital-audits", "title": "Digital audits (GoBD/GDPdU) - Business Central | Microsoft Learn", "accessed_at": "2026-01-16T21:12:47.022491+00:00", "status_code": 200, "content_type": "text/html"}', '2026-01-16T21:12:47.023162+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (12, 'website', 'https://www.getpliant.com/en-us/blog/what-is-gobd/', 'Is your business GoBD compliant?', NULL, 'Is your business GoBD compliant?
home page
Products
OPTIMIZE CARD PAYMENTS
Payment Apps
Pro API
BUILD YOUR OWN PROGRAM
Cards-as-a-Service
CardOS
Discover Payment Apps
→
Conveniently manage all your company''s card payments
Features
Real time monitoring
Receipt management
Spend control
Accounting automations
Multi-currency accounts
Benefits
Integrations
Discover Pliant Pro API
→
Automate your payment processes via API
Features
Card issuance & management
Transaction insights
Accounting optimization
Member management
Custom integrations
Discover Cards-as-a-Service (CaaS)
→
Build your own custom credit card offering
Features
Card issuance & management
Advanced data capabilities
Ready-made UI
Compliance & security
Dedicated support
CaaS API
Integrations
Discover CardOS
→
Launch best-in-class credit card programs for banks
Features
Accounting automation & integrations
Next-generation financial infrastructure
Modular architecture & detailed customization
Scalable back-office tools
Flexible integration
Cards
Cards
See the advantages of all our different credit cards
Use Case
Payment Technology
Travel Purchasing Cards
Lodge Cards
Fleet Cards
Employee Benefit Cards
Insurance Claim Cards
Emergency Disbursement Card
Physical Cards
Premium Cards
Virtual Cards
Single-use Cards
Solutions
Solutions
How customers from key industries benefit from Pliant
OPTIMIZE CARD PAYMENTS
Corporations
E-commerce
Marketing agencies
Resellers
SaaS
Travel industry
BUILD YOUR OWN PROGRAM
ERP
Invoice management
Travel expense management
Specialised lending
Banking
Insurance payments
Recent customer stories
All customer stories
→
Salabam Solutions
“Pliant Pro API is a key asset to our travel booking platforms.”
Travel
Circula
"Circula will process €100 million in card spend this year"
Travel expense management
acocon
"Thanks to Pliant, we''ve been able to win customers from other Atlassian partners.”
Resellers
Resources
Resources
All the detailed information about Pliant for both visitors and customers
Pricing
Exchange rates
Help center
Blog
Events
FAQ
Press
Careers
Contact
Revenue calculator
Recent blog posts
All blog posts
→
Building Real-Time, Reliable Notifications with Server-Side Events: A Case Study from Fintech
Virtual Credit Cards for Employees: What You Need to Know
TMC-tailored virtual credit cards: Cashback at your fingertips
What is a Card Issuance Provider? And who would benefit from issuing their own credit cards?
Developers
Developers
Build your all-in-one credit card solution with Pliant
Pro API
Documentation
Changelog
API Reference
Status
CaaS API
Documentation
Changelog
API Reference
Status
Developers Starter Guide
Sales
:
+1 (917) 540 4658
Login
Get started
Business
5 min read
Is your business GoBD compliant?
Whether you''re interested in starting a business or expanding your operations to Germany, making sure your company is GoBD compliant should be one of your top priorities.

In this quick read, we’ll address the most relevant aspects you should consider before taking the big leap into the German market.
Duline Theogene
on
12/6/2022
Table of content
What is GoBD?
Who regulates the GoBD?
What are the GoBD principles?
What happens if you do not comply with GoBD?
Ways to present data to auditors according to the GoBD
Final Thoughts
💡
Quick note:
For more detailed information and legal and financial advice, contact your tax advisor.
What is GoBD?
Conveniently, GoBD is short for
Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff.
Which in English translates to “
Basic Principles on The Proper Keeping and Storage of Financial Books, Recordings, and Documents in Electronic Form as Well as Data Access.”
Who regulates the GoBD?
The German Federal Ministry of Finance has been regulating these principles since 2014. And in January 2020, an updated version came into effect.
What are the GoBD principles?
As mentioned previously, in Germany, the principles of proper accounting are known as ''GoB''; the added ''D'' refers to ''digital data'' or ‘electronic form.’
They''re based on the
German Fiscal Code (AO)
, and they make the digitization and management of financial documentation possible.
Their main purpose is to ensure that both entrepreneurs and companies maintain and process their physical and digital
financial records
, also known as ''books''
, in
tip top shape.
The GoBD principles can be listed and summarized as follows:
1. Traceability and Verifiability
As the name implies, your financial records must be easy to track and verify. In order to do so, you should have a
reliable invoice management system
.
We also recommend that you prepare a documentation process describing how your tax information is or will be handled, including:
Internal Control System (ICS)
all types of company expenses and revenue streams
relevant financial processes
the way in which receipts and invoices are handled,
etc.
This will be extremely useful if you plan to hire a third party to manage your finances as it will give them a clear picture of the financial state of your company and the procedures you have in place to fulfill
your tax obligations.
2. Principles of Truth
Your tax auditor must be able to ensure that all your transactions are governed by honest practices and therefore processed in reliable systems, always abiding by the GoBD standards.
3. Clarity and Continuous Recording
Keep in mind that all your physical and digital financial records must remain available from 6 to 10 years in case you''re required to submit them for review.
4. Completeness
Each document subject to review must be recorded in full.
Invoices, for example, must contain at least the following data:
Date of issuance
Invoice sequential number
VAT business information
Business and customer address
Legal entity (GmbH, AG, KG, OHG, e.K)
Tax number
Complete and detailed description of sale or service provided
VAT amount or VAT exception if applicable (i,e. zero VAT rate or reverse charge)
5. Individual Recording Obligation
Even if you''re planning to hire a third party company or software, to handle your bookkeeping, bear in mind that only you--as the owner of the company, will be responsible for being compliant at the time of the audit as stipulated by article 33 of the AO (German Fiscal Code).
6. Correctness
As you can imagine, your tax-relevant data must be double-checked before submitting it for audit review to avoid any mishaps.
7. Timely Bookings and Records
It is critical that your accountant or finance teams educate your employees around this important issue.
Your staff should scan and submit invoices immediately after an expense is made
so that at the end of the month all receipts are recorded and available in your
accounting system
with the expenses made in that period.
8. Order and Immutability
Your records should be kept in an orderly and chronological fashion. This way, a tax auditor will be able to access them quickly.
Immutability means that no receipt or invoice should be altered or overwritten.
What happens if you do not comply with GoBD?
In the event that your company does not comply with the GoBD principles, your documents will be subject to a second audit or you could find yourself in a
back tax issue.
This is all likely caused by:
not reporting correct taxes the previous year,
not filing a tax return,
reporting incorrect income, or
missing a deadline.
Ways to present data to auditors according to the GoBD
According to the latest update of these principles, companies or entrepreneurs can present digital tax-relevant data  in three ways:
1. Direct access (Z1)
2. Indirect access (Z2)
3. Data medium provision (Z3)
Direct access (Z1)
allows the auditor to have immediate digital access to the company''s records or systems to be audited.
Since the auditor won''t be responsible for any type of modification or error, we recommend that you give them ''read-only'' permission to your records. This way, they won''t be able to edit or modify any data by mistake.
With
indirect access (Z2)
, the auditor must be physically present at the company. There, you will be give them digital access to all necessary documents.
Data medium provision (Z3)
grants the information to be audited on a digital medium stipulated by the auditors themselves. This can be in a type of data analysis software normally used by auditors (i.e,
IDEA
).
Final Thoughts
Tax and data compliance is an issue of great concern for every entrepreneur exploring a new business venture. More specifically if we’re referring to business owners looking to expand into the German market.
We hope we have answered all your initial questions about the GoBD principles
, and we remind you to contact your tax advisor if you want to know more detailed information tailored to your business'' needs.
If you need a smart expense management solution for your company, we suggest you
book a demo
with our team.
Duline Theogene
LinkedIn icon
Content Marketing Manager
Table of content
What is GoBD?
Who regulates the GoBD?
What are the GoBD principles?
What happens if you do not comply with GoBD?
Ways to present data to auditors according to the GoBD
Final Thoughts
Are you ready for modern credit cards?
Talk to a Pliant Expert Today!
Thank you for your interest.
We will send you more information.
Something went wrong!
Please try again.
Recent blog posts
All blog posts
Building Real-Time, Reliable Notifications with Server-Side Events: A Case Study from Fintech
In this post, we’ll explore how we designed and implemented a scalable, event-driven architecture based on Server-Side Events (SSE) to handle these kinds of user-facing workflows in real time.
Tech at Pliant
4 min read
Virtual Credit Cards for Employees: What You Need to Know
A modern virtual card solution for employees is secure, transparent, and saves time for management, employees, and accountants through streamlined digital processes.
Credit cards
11 min read
TMC-tailored virtual credit cards: Cashback at your fingertips
Travel Management Companies (TMCs) make business travel easy for their clients. A powerful modern corporate credit card solution ensures that TMCs’ internal processes and operations run equally smoothly.
Travel
5 min read
What is a Card Issuance Provider? And who would benefit from issuing their own credit cards?
In the constantly evolving landscape of financial services, card issuers are playing an increasingly important role. But what exactly does a card issuance provider do? Simply put, these entities are the engine behind the creation and management of payment cards. They offer businesses the tools to launch their own branded cards, either physically or digitally, opening up new opportunities for revenue, customer engagement, and financial management.
CaaS
5 min read
Could Your Company Issue Credit Cards? 3 Industries That Could Benefit from Cards-as-a-Service
If you’re looking to expand your profit margins, and your customer base, by adding financial services to your portfolio, a credit card issued and branded by your company is certainly a goal to aspire to. However, without any experience of offering financial services, you might be wondering about the best way to issue credit cards and bolster your revenue streams. Fortunately, Cards-as-a-Service (CaaS) is the simple, effective option that brings your own card program within reach. Let’s look at the industries best suited to issue credit cards and whether your company could benefit too.
CaaS
9 min read
What Is Embedded Finance? And How Could It Benefit Your Business?
As TechCrunch so aptly put it, embedded finance is having a moment: banking, payments, and more are being continually integrated into the apps and platforms you already use. You’d be forgiven for thinking that controlling the means of payment would be a goldmine because, well… it is. In fact, more companies than ever are aiming to blur the lines between product and payment, and if you’ve found this post, yours might be among them.
CaaS
11 min read
All blog posts
Payment Apps
Discover Payment Apps
Real-time monitoring
Receipt management
Spend control
Accounting automations
Multi-currency accounts
Benefits
Integrations
Pro API
Discover Pliant Pro API
Card issuance & management
Transaction insights
Accounting optimization
Member management
Integrations
Custom integrations
CaaS
Discover Cards-as-a-Service
Card issuance & management
Advanced data capabilities
Ready-made UI
Compliance & security
Dedicated support
CaaS API
Integrations
Card OS
Discover Card OS
Accounting automation & integrations
Next-generation financial infrastructure
Modular architecture & detailed customization
Scalable back-office tools
Flexible integration
Cards
Physical cards
Premium cards
Virtual cards
Single-use cards
Travel purchasing cards
Fleet cards
Benefit cards
Insurance claim cards
Solutions
Corporations
E-commerce
Marketing agencies
Resellers
SaaS
Travel
ERP
Invoice management
Travel expense management
Specialised lending
Insurance payments
Customer stories
Resources
Pricing
Help center
Blog
Events
API Documentation
Exchange rates
FAQ
Developers
Company
About Pliant
Careers
HIRING
Press
Contact
Follow us on
linkedin
Pliant''s Youtube channel
Download on the App Store
Download Pliant App on the Google Play Store
© 2020 –
2026
Pliant GmbH
© 2020 –
2026
Pliant GmbH
Pliant is certified as a
Payment Card Industry (PCI) Data Security Standard
service provider and has achieved
ISO Certificate 27001-2022.
Pliant is a financial technology company, not an FDIC Insured bank. Banking services provided by Coastal Community Bank, Member FDIC. FDIC insurance only covers the failure of an FDIC insured bank. FDIC insurance is available through pass-through insurance at Coastal Community Bank, Member FDIC, if certain conditions have been met. The Pliant Corporate Credit Card is issued by Coastal Community Bank, Member FDIC pursuant to a license from Visa U.S.A and may be used everywhere Visa is accepted.
Imprint
Privacy Policy
Coastal Community Bank Privacy Policy
Privacy Settings
Global (English)', '1f3cc1170dfe5d96613a9f051834afb53c0b6d0bc2b1092d42df041ead33f070', '{"url": "https://www.getpliant.com/en-us/blog/what-is-gobd/", "title": "Is your business GoBD compliant?", "accessed_at": "2026-01-16T21:13:17.715310+00:00", "status_code": 200, "content_type": "text/html; charset=utf-8"}', '2026-01-16T21:13:17.717390+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (13, 'website', 'https://en.fileee.com/digitalisierung/gobd', 'GoBD briefly explained - fileee', NULL, 'GoBD briefly explained - fileee
SECURE THE BEST DEALS
TO THE OFFERS
NEW YEAR DEAL
20 % Sparen
// Code:
ORDNUNG26
HIER ENTDECKEN
SECURE THE BEST DEALS
TO THE OFFERS
NEW YEAR DEAL
20 % Sparen
// Code:
ORDNUNG26
HIER ENTDECKEN
Product
Features
All features at a glance
Pricing
fileee Spaces
fileee Appstore
fileee Partner
Products
fileeeBox
fileeeDIY
Solutions
Solutions
for you
for families
About fileee
About fileee
Our mission
Security
Sustainability
Artificial intelligence
Career with fileee
Resources
Info
Blog
Help centre
Become a partner
Private
Business
LOGIN
Register for free
Register for free
Product
Features
All features at a glance
Business pricing
fileee teams
fileee Appstore
fileee Partner
Products
fileeeBox
fileeeDIY
fileee Conversations
Solutions
Solutions
for self-employed
for clubs
for small businesses
for tax consultants
About fileee
About fileee
Our mission
Security
Sustainability
Artificial intelligence
Career with fileee
Resources
Info
Blog
Help centre
Media library
References
Become a partner
Digitization
Procedural documentation
NEW
E-bill
NEW
Digital document management
NEW
Digital personnel file
NEW
Digital invoice receipt
NEW
GoBD
Annual financial statements
Paperless office
Audit-proof archiving
Private
Business
LOGIN
Register for free
Register for free
Register for free
Private
Business
LOGIN
Product
Features
All features at a glance
Pricing
fileee Spaces
fileee Appstore
fileee Partner
Products
fileeeBox
fileee DIY
Solutions
Solutions
for you
for families
About fileee
About fileee
Our mission
Security
Sustainability
Artificial intelligence
Career with fileee
Resources
Info
Blog
Help centre
Become a partner
Register for free
Private
Business
LOGIN
Product
Features
All features at a glance
Pricing
fileee teams
fileee Appstore
fileee Partner
Products
fileee Conversations
fileeeBox
fileee DIY
Solutions
Solutions
for self-employed
for clubs
for small businesses
for tax consultants
Templates
No items found.
About fileee
About fileee
Our mission
Security
Sustainability
Artificial intelligence
Career with fileee
Resources
Info
Blog
Help centre
Media library
References
Become a partner
Digitization
Procedural documentation
NEW
E-bill
NEW
Digital document management
NEW
Digital personnel file
NEW
Digital invoice receipt
NEW
GoBD
Annual financial statements
Paperless office
Audit-proof archiving
We have updated our pricing
. Nothing has changed for private customers. Brand new: fileee Business
WATCH NOW
â
GoBD
The principles for the proper keeping and storage of books, records and documents in electronic form and for data access explained.
Definition of the GoBD
The abbreviation GoBD stands for: Principles for the proper keeping and storage of books, records and documents in electronic form and for data access. These principles, issued by the German Federal
Ministry of Finance
(BMF), contain criteria and guidelines that companies must fulfill when using electronic accounting.
GoBD briefly explained:
Requirements and background of the GoBD
As part of the GoBD, audit-proof storage describes the storage of digital data in terms of correctness, completeness, security, availability, traceability, immutability and access protection. These are to be understood as central requirements for audit security and are recorded in chapter three under "General requirements" of the GoBD guidelines:
Table of contents
What are the GoBD?
To whom do the GoBD apply?
Violations of the GoBD and consequences of non-compliance
Which documents are affected by the GoBD?
GoBD-compliant accounting with the help of a DMS
What else do I need to consider when complying with the GoBD?
Book free demo appointment
Since the demands on companies and IT-supported systems are becoming greater with increasing digitalization, the GoBD have also been adapted. In the latest version dated November 28, 2019, for example, topics such as
mobile scanning
or
cloud systems
are also included. A
mobile scan
is another import path into the system when capturing receipts.
However, the GoBD itself is not a legal text, but formally describes the criteria on the basis of which a tax audit takes place. What are the requirements and background of the GoBD? We explain briefly:
1. what are the GoBD?
Even if the
letter from the BMF
reads like a legal text, these principles formally "merely" set out criteria according to which it can be determined in a tax audit whether a company has complied with the proper keeping of books or records. Thus, compliance with the GoBD is in the hands of the taxpayer and the verification of this is in the hands of the tax office.
The GoBD describe the following points:
Use of GoBD-compliant software
Audit-proof archiving
Procedural documentation
GoBD-compliant operation
Important:
In this context, the retention periods specified in Section 147 (3) of the German Fiscal Code (AO) of 6 or 10 years must always be observed (to be found under Chapter 3, Item 27 of the GoBD Guidelines).
These four rules must be observed in digital archiving in order to be considered GoBD-compliant:
Inalterability, completeness, traceability and availability.
2 To whom do the GoBD apply?
All taxable entrepreneurs who generate profit income in any form are equally obliged to comply with the GoBD.
Based on the German Fiscal Code (Section 90 (3), items 141 to 144, AO ) and the individual tax laws (Section 22 UStG, Section 4 (3) sentence 5, Section 4 (4a) sentence 6, Section 4 (7) and Section 41 EStG), the GoBD obligate not only companies that are required to keep accounts but also self-employed persons, freelancers and small entrepreneurs who are not required to keep accounts to retain all tax-relevant data in accordance with the specified requirements.
Anyone who can ensure that the requirements anchored in the
GoBD
are met over the entire period of the retention periods is acting in
compliance with the GoBD
.
3. violations of the GoBD and consequences of non-compliance
In the event of non-compliance with the principles or violations of the GoBD, there are various consequences depending on the extent. If deficiencies in compliance with the GoBD are discovered during a tax audit and in particular in such a way that further deficiencies result from this, such as amounts being falsified or concealed as a result, this may have consequences under criminal tax law. As described above, the tax office proceeds according to the criteria of the GoBD during an audit and, depending on the extent, can, for example, demand tax arrears or interest on arrears, make estimates or even impose fines.
4. which documents are affected by the GoBD?
The GoBD concern all tax-relevant data. According to Â§ 147 para. 1 AO, these are as follows:
Books
Records
Inventories
Financial statements
Business and commercial letters
Important areas here are: Financial accounting, payroll accounting, cost accounting, bank accounts, asset accounting.
In addition, other documents are subject to retention and are therefore affected by the GoBD if they are relevant to the business. This also includes e-mails. E-mails must be archived and are subject to retention if the text of the e-mail contains relevant information for an invoice, for example. The e-mail does not have to be stored additionally if it only serves as a transmission medium and does not contain any relevant information.
5. GoBD-compliant accounting with the help of a DMS
The GoBD does not stipulate which system is to be used. Electronic archiving is primarily technology-neutral. However, it is advisable to use a DMS that is designed in accordance with the GoBD guidelines. The GoBD states: "The storage of data and electronic documents in a file system does not regularly meet the requirements of immutability unless additional measures are taken to ensure immutability" (GoBD chapter 8, point 110).
In addition to the criterion of immutability (GoBD chapter 3.2.5), the other criteria for
audit-proof archiving
must be met: Traceability and verifiability (GoBD chapter 3.1), completeness (GoBD chapter 3.2.1), accuracy (GoBD chapter 3.2.2), timely posting and records (GoBD chapter 3.2.3) and order (GoBD chapter 3.2.4). Here, audit-proof archiving acts as a prerequisite for GoBD-compliant archiving.
â
A DMS does not ensure audit compliance or compliance with the GoBD on its own, but can only be seen as an aid to implementation. The services of the various systems also differ.
â
Tip:
A comprehensive list of the requirements for a DMS in relation to GoBD compliance can be found at Bitkom in the "
GoBD checklist for document management systems
".
Best practice: How Winzerhof Wirges manages GoBD-compliant documents with fileee
: Digitizing the family business while complying with the GoBD guidelines - this was the challenge Andreas Wirges faced with his winery. In this webinar, we will show you how to quickly bring structure to your company''s bookkeeping and save valuable time while still complying with all legal requirements. Watch the
webinar now.
â
What else do I need to consider when complying with the GoBD?
In addition to the requirements for the DMS, the GoBD also regulates the requirements for the taxpayers, i.e. the internal recording and processing procedures as well as the behavior of the users.
The company itself is responsible for complying with the GoBD. This includes the following:
First, a digital system must be selected that meets the criteria of the GoBD with regard to digital filing.
Receipts must be captured in a timely manner (for example, via scan or import) and uploaded to the system. This can be done manually or automatically. It is important that the chronological sequence can be traced here as well. For a timely capture, one speaks of 8 to 10 days.
Likewise, procedural documentation must be created. This documentation must contain information on the data processing procedure: "The procedural documentation usually consists of a general description, user documentation, technical system documentation and operational documentation" (GoBD Chapter 10.1, item 153).
In addition, responsibilities must be clarified within the team: Who approves the data? Who checks the data?
The system itself is responsible for the availability and visibility of the necessary data, including history, as well as for
audit-proof archiving
and automatic creation of electronic files in terms of proper structure and coherence.
fileee BUSINESS
meets the requirements of the GoBD and supports your company in the audit-proof archiving of your electronic documents.
Any questions?
GoBD - Frequently asked questions
What does GoBD mean?
The GoBD are the "Principles for the proper keeping and storage of books, records and documents in electronic form as well as for data access" and were rewritten and defined by the Federal Ministry of Finance on November 28, 2019. These principles regulate the requirements to be met by digitally mapped processes from the perspective of the tax authorities.
What is revision security?
"Revision" means "alteration", "correction" or also "revision" and is understood in connection with "security", i.e. protection against it, in such a way that something is protected from change in this sense. In the context of documents, this term is used both in the technical and organizational area in the context of the electronic storage of data. The GoBD regulates the requirements for audit-proof storage.
What is the difference between GoBD and audit security?
The GoBD is the set of rules with the requirements for audit-proof storage and how audit security is given. Audit security is therefore a part of the GoBD and whoever acts in conformity with the GoBD, acts audit secure at the same time. In addition, the GoBD regulates, for example, the "data security" under the point 103 in chapter 7: "The taxpayer has to protect his DP system (...) against unauthorized inputs and changes (e.g. by access and access controls)". (from:
https://www.bundesfinanzministerium.de/Content/DE/Downloads/BMF_Schreiben/Weitere_Steuerthemen/Abgabenordnung/2019-11-28-GoBD.pdf?__blob=publicationFile&v=13)
What does GoBD compliance mean?
The implementation of the GoBD. Anyone who can ensure that the requirements anchored in the
GoBD
are met acts in
compliance with the GoBD
.
When is a DMS GoBD-compliant?
A DMS is GoBD-compliant if audit-proof storage is ensured and the GoBD requirements are met. The GoBD
checklist from Bitkom
breaks these down again directly for document management systems and explains the various points in connection with implementation.
All-around secure and GoBD-compliant with fileee Business
Start now with fileee Business
Book a demo
Get started straight away
Product
fileee Spaces
fileeeBox
fileee Appstore
fileee Partner
Solutions
Pricing
INFORMATION
Security
Sustainability
Artificial intelligence
Our Story
Our mission
Jobs
Magazine
SERVICE
Help centre
Support request
Login
DOWNLOAD
ABOUT FILEEE
Product
fileee Box
fileee Spaces
fileee Appstore
fileee Partner
Solutions
Pricing
INFORMATION
Security
Sustainability
Artificial intelligence
Document management
Our Story
Our mission
Jobs
Blog
SERVICE
Help centre
Support request
DOWNLOAD
Private
Business
Â© 2025 fileee. All Rights Reserved.
TOS
Performance specification
Data protection
Imprint
Product
fileee teams
fileeeBox
fileee Appstore
fileee Partner
fileee Conversations
Solutions
Pricing
INFORMATION
Security
Sustainability
Artificial intelligence
Document management
Our Story
Our mission
Jobs
Magazine
SERVICE
Help centre
Support request
Partner options
Login
DOWNLOAD
ABOUT FILEEE
Product
fileee Box
fileee teams
fileee Appstore
fileee Partner
Solutions
Pricing
INFORMATION
Security
Sustainability
Artificial intelligence
Our Story
Our mission
Jobs
Blog
SERVICE
Help centre
Support request
Partner options
DOWNLOAD
Private
Business
Â© 2025 fileee. All Rights Reserved.
TOS
Performance specification
Data protection
Imprint
Our website uses cookies
We at fileee want to offer you relevant content. For this purpose, we store information about your visit in so-called cookies.
              Click
here
if you only want to accept technically necessary cookies. Detailed information on data protection can be found
here
.
Agree', '52f2e4c1f117264f5101d0021b2cbb0bc25bdcdf2cfef0d29577069e4dd7e69b', '{"url": "https://en.fileee.com/digitalisierung/gobd", "title": "GoBD briefly explained - fileee", "accessed_at": "2026-01-16T21:13:18.036772+00:00", "status_code": 200, "content_type": "text/html"}', '2026-01-16T21:13:18.037608+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (14, 'website', 'https://www.lexology.com/library/detail.aspx?g=19c9005b-3c4f-4a48-b585-90fa69c758c9', 'What You Need to Know About the GoBD - Lexology', NULL, 'What You Need to Know About the GoBD - Lexology
Skip to content
Search Lexology
PRO
Events
Awards
Explore
Login
Register
Home
Latest intelligence
Legal research
Regulatory monitoring
Practical resources
Experts
Learn
Awards
Influencers
Lexology Index Awards 2025
Lexology European Awards 2026
Client Choice Dinner 2026
Lexology Compete
About
Help centre
Blog
Lexology Academic
Login
Register
PRO
Compete
Lexology
Article
﻿
Back
Forward
Save & file
View original
Forward
Print
Share
Facebook
Twitter
LinkedIn
WhatsApp
Follow
Please login to follow content.
Like
Instruct
add to folder:
My saved
(default)
Read later
Folders shared with you
Register now
for your free, tailored, daily legal newsfeed service.
Find out more about Lexology or get in touch by visiting our
About
page.
Register
What You Need to Know About the GoBD
Association of Corporate Counsel
Germany
,
Global
,
USA
September 11 2017
Authored by:
K Royal,
technology columnist for www.AccDocket.com, and vice president, associate general counsel of privacy, and compliance/privacy officer at CellTrust Corp.
This article was published as part of ACC’s “This Week in Privacy” series, a new column for in-house counsel who need advice in the privacy and cybersecurity sectors.
Question:
With all the data protection reform going on in Europe, I heard about something called the GoBD, which pertains to tax papers. What is that?
Answer:
Unlike the General Protection Data Regulation (GDPR), the GoBD is not a well-known or oft-discussed topic. The German GoBD, or the “basic principles on the proper keeping and storage of financial books, recordings, and documents in electronic form as well as data access” (Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff), became effective a little over two years ago and is specifically related to tax documentation. It replaced two prior requirements: one from 1995, the GoBS (principles of proper DV-based accounting systems), and one from 2001, the GDPdU (principles of data access and verifiability of digital documents).
The GoBD greatly increases the reach of the German Ministry of Finance, because not only are there many types of documents, records, and data that can be linked to tax purposes, but also because the Ministry requires a years’ worth of continuous documentation. The documentation is especially critical in cash-based businesses, like hair salons and restaurants, because cash transactions is highly subject to manipulation and inaccurate reporting.
In this digital age, many documents and records are created or retained electronically. Some records are still required to be kept in original paper, such as donation receipts and capital gains certificates. Otherwise, companies often desire to reduce the paper burden and retain digitized copies.
The GoBD facilitates that desire, but requires that the auditability and traceability of the original transactions remain. For example, a PDF/A-3 comprises both an image and XML filed linked to the information contained in the image. The tx authorities would need to be able to audit that electronic file. If it is transformed into a JPG, TNG, or PNG, then the XML information would be lost.
The GoBD also contains timeframe restrictions — cash transactions must be captured daily and non-cash transactions must be captured every 10 days. Certain transactions are permitted to be captured on a monthly basis, but there are limitations and requirements around regular scheduling of these digitization actions. The two specific provisions in the GoBD around electronic record-keeping are data immutability and security.
For more guidance on the GoBD, please visit one of the following links:
VGD
,
SMACC
, or
Bundesministerium der Finanzen
.
For further reading, download
ACC’s White Paper on “What Every GC Needs to Know About Third Party Cyber Diligence
.”
The Association of Corporate Counsel (ACC) is a global legal association that promotes the common professional and business interests of in-house counsel who work for corporations, associations and other private-sector organizations through information, education, networking opportunities and advocacy initiatives. With more than 45,000 members in 85 countries, employed by over 10,000 organizations, ACC connects its members to the people and resources necessary for both personal and professional growth. By in-house counsel, for in-house counsel.®
﻿
Back
Forward
Save & file
View original
Forward
Print
Share
Facebook
Twitter
LinkedIn
WhatsApp
Follow
Please login to follow content.
Like
Instruct
add to folder:
My saved
(default)
Read later
Folders shared with you
Filed under
Germany
Global
USA
IT & Data Protection
Law Department Management
Tax
Association of Corporate Counsel
Topics
Information privacy
Laws
GDPR
Interested in contributing?
Get closer to winning business faster with Lexology''s complete suite of dynamic products designed to help you unlock new opportunities with our highly engaged audience of legal professionals looking for answers.
Learn more
Professional development
Implementing & Maintaining Data Retention & Data Management Policies - Learn Live
MBL Seminars | 1.5 CPD hours
Online
18 March 2026
Mastering Data Processing Agreements - Drafting, Negotiating & Mitigating Risk- Learn Live
MBL Seminars | 4 CPD hours
Online
12 May 2026
Microsoft Outlook - Going Beyond the Basics - Learn Live
MBL Seminars | 2 CPD hours
Online
20 January 2026
View all
Related practical resources
PRO
How-to guide
How-to guide: How to deal with a GDPR data breach (UK)
How-to guide
How-to guide: How to establish a valid lawful basis for processing personal data under the GDPR (UK)
How-to guide
How-to guide: How to ensure compliance with the GDPR (UK)
View all
Related research hubs
GDPR
USA
Germany
Tax
IT & Data Protection
Resources
Daily newsfeed
Panoramic
Research hubs
Learn
In-depth
Lexy: AI search
Scanner
Contracts & clauses
Lexology Index
Find an expert
Reports
Research methodology
Submissions
FAQ
Instruct Counsel
Client Choice 2025
More
About us
Legal Influencers
Firms
Blog
Events
Popular
Lexology Academic
Legal
Terms of use
Cookies
Disclaimer
Privacy policy
Contact
Help centre
Contact
RSS feeds
Submissions
Login
Register
Follow on X
Follow on LinkedIn
© Copyright 2006 -
2026
Law Business Research', 'f01059dc7cc18dc1abf30358f1404b279db05f9ab16d92a7e56de3e2cf077da5', '{"url": "https://www.lexology.com/library/detail.aspx?g=19c9005b-3c4f-4a48-b585-90fa69c758c9", "title": "What You Need to Know About the GoBD - Lexology", "accessed_at": "2026-01-16T21:13:18.437872+00:00", "status_code": 200, "content_type": "text/html; charset=utf-8"}', '2026-01-16T21:13:18.440232+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (15, 'website', 'https://easy-software.com/en/glossary/data-protection-the-cornerstones-of-digital-sovereignty-gdpr-gobd/', 'Data Protection (GDPR & GoBD) – Explanation – Glossary', NULL, 'Data Protection (GDPR & GoBD) – Explanation – Glossary
Skip to content
easy portal
contact
×
language
Global (English)
Deutschland | Schweiz
Contact
Menu
Menu
Solutions
Application Areas
Know-how
Service & Support
Partner
About easy
language
Global (English)
Deutschland | Schweiz
Contact
×
Solutions
Powerful
ECM
solutions
Digital archiving, accounts payable, contracting, and HR management systems are available as on-premises, private cloud, cloud-native, or hybrid solutions. Intelligent workflows and AI services automate document-based processes.
easy
archive
Compliant archiving
easy
invoice
Secure invoice management
easy
DMS
Efficient document management
easy
contract
Transparent contract management
easy
hr
Smart HR management
SAP
Add-ons
Experience digitization easily and quickly with third-party systems, such as SAP
®
. easy provides SAP-certified add-ons.
learn more
Application Areas
references
Banks and insurances
Energy industry
Manufacturing and production
Public service and associations
View all industries
application areas
Archiving
Document Capture
Procurement Processes
Digital Personnel File
Digital Signature
Document Management
Incoming Invoices
Electronic File
Contract Management
Workflows
successful customers
Read success stories from some of the 5400+ easy customers who are digitally transforming their businesses with easy products.
case studies
Know-how
Knowledge
Browse the blog. Use free white papers, checklists and guidelines and expand your knowledge in webinars and at events.
Newsroom
Blog
Press & News
Glossary
Events & Whitepaper
Events & Live Webinars
Knowledge base
Guides & Checklists
Webinar Recordings
Whitepaper
Digital accounts payable processing
Find out in our white paper how your company can benefit from electronic incoming invoice processing.
free whitepaper
Service & Support
Resources
Develop your potential with the easy Academy, optimize your business with Managed Services and get help from professional easy Support.
Service
easy Academy
Managed Services
Support
Support
As a direct customer, you can benefit from our support.
support
Partner
Partner Network
Benefit from our experience as a software manufacturer for ECM, SAP, Cloud and SaaS solutions. We want to grow together with our software partners.
Partners
Become a partner
Find partners
easy Portal
Become a partner
Become part of our partner community and expand your business model with the successful easy product portfolio.
become a partner
About easy
easy group
With over 30 years of experience, easy software is one of the market leaders for archiving, ECM, and DMS software solutions in the German-speaking region.
About Easy
Company
Press & News
Customer Stories
Contact Us
If you have any questions, just let us know.
contact us
Solutions
easy
archive
Compliant archiving
easy
invoice
Secure invoice management
easy
contract
Transparent contract management
easy
hr
Smart HR management
easy
DMS
Efficient document management
SAP
Add-ons
Experience digitization easily and quickly with third-party systems, such as SAP
®
. easy provides SAP-certified add-ons.
learn more
Application Areas
references
Banks and insurances
Energy industry
Manufacturing and production
Public service and associations
View all industries
application areas
Archiving
Document Capture
Procurement Processes
Digital Personnel File
Digital Signature
Document Management
Incoming Invoices
Electronic File
Contract Management
Workflows
successful customers
Read success stories from some of the 5400+ easy customers who are digitally transforming their businesses with easy products.
case studies
know-how
Knowledge
Browse the blog. Use free white papers, checklists and guidelines and expand your knowledge in webinars and at events.
Newsroom
Blog
Press & News
Glossary
Events & Whitepaper
Events & Live Webinars
Knowledge base
Guides & Checklists
Webinar Recordings
Whitepaper
Digital accounts payable processing
Find out in our white paper how your company can benefit from electronic incoming invoice processing.
free whitepaper
Service & Support
Resources
Develop your potential with the easy Academy, optimize your business with Managed Services and get help from professional easy Support.
Service
easy Academy
Managed Services
Support
Support
As a direct customer, you can benefit from our support.
support
Partner
Partner Network
Benefit from our experience as a software manufacturer for ECM, SAP, Cloud and SaaS solutions. We want to grow together with our software partners.
Partners
Become a partner
Find partners
easy Portal
Become a partner
Become part of our partner community and expand your business model with the successful easy product portfolio.
become a partner
About easy
easy group
With over 30 years of experience, easy software is one of the market leaders for archiving, ECM, and DMS software solutions in the German-speaking region.
About Easy
Company
Press & News
Customer Stories
Contact Us
If you have any questions, just let us know.
contact us
easy portal
Glossary
Data Protection: The Cornerstones of Digital Sovereignty (GDPR & GoBD)
Data protection is a central component of responsible corporate management in today’s digital world. Data protection measures encompass all activities that actively shield personal data from misuse and safeguard the privacy of the affected individuals.
For German companies, two sets of regulations are indispensable in this context, establishing legal certainty and order: the General Data Protection Regulation
(GDPR
), and the Principles for the proper management and retention of books, records and documents in electronic form as well as for data access (
GoBD
). The latter aims for tax law certainty.
1. General Data Protection Regulation (GDPR)
The
GDPR
is a regulation of the European Union that has been in force since May 25, 2018. It regulates the protection of personal data within the EU and aims to protect the rights and freedoms of natural persons. The
GDPR
stipulates how companies, authorities, and other organizations must handle personal data.
The following principles are particularly important:
Lawfulness, Fairness, and Transparency
: Data may only be processed lawfully and in a manner that is comprehensible to the data subject.
Purpose Limitation
: Data may only be collected for specified, explicit, and legitimate purposes.
Data Minimization
: Only as much data may be collected as is necessary for the respective purpose (
Need-to-Know Principle
).
A
ccuracy
: Data must be factually correct and kept up to date.
Storage Limitation
: Data may only be stored for as long as is necessary for the purposes for which it is processed.
Integrity and Confidentiality
: Appropriate technical and organizational measures must protect data from unauthorized or unlawful processing, as well as from accidental loss, destruction, or damage.
2. Principles for Proper Bookkeeping (GoBD)
The
GoBD
are a set of rules issued by the German Federal Ministry of Finance that have been in effect since January 1, 2015. They stipulate how electronic books, records, and documents must be managed and retained. The objective of the
GoBD
is to ensure the traceability and verifiability of tax-relevant data at all times.
Key aspects of the GoBD for digital documentation include:
Immutability (Audit Compliance
): Once captured, data may not be subsequently altered without the change being documented.
Traceability and Verifiability
: All business transactions must be clearly and comprehensibly documented.
Orderliness
: The system must retain data systematically and in an orderly manner.
Security
: Data must be protected from loss and unauthorized access.
Software Solutions: Compliance Goes Digital
In day-to-day business, organizations must continuously ensure that all stored and processed data comply with these legal requirements (GDPR and GoBD). Compliance here is not a burden but a
hallmark of quality
for internal processes.
A well-integrated
Document Management System
(DMS) is the key to facilitating these compliance tasks:
Automated Compliance:
The DMS enables automated checks, deletion periods, and reports. It ensures that all documents are correctly archived and sensitive data is protected.
Transparency and Security
: Through automatic archiving, logging, and encryption of sensitive documents, the system ensures that all data complies with legal requirements and remains traceable at all times.
The use of modern
ECM solutions
demonstrably reduces actual and potential data protection breaches. Simultaneously, it improves internal processes and creates higher transparency far beyond mere data protection.
Outlook and Expert Support
Implementing a system for better data protection brings specific challenges: the complexity of legal requirements, the integration of compliance into the corporate culture, and continuous adaptation to new regulations.
Data protection is an indispensable component of modern corporate management. Compliance with legal and internal requirements protects companies from legal risks and strengthens their reputation. Especially in the ECM industry, a robust data protection management system is crucial to ensure the integrity and security of information.
Mastering Compliance Challenges
To master these challenges with the right software and achieve compliance goals, experts are available at any time for a personal consultation. The use of modern technologies is the decisive factor for success.
contact us
easy
archive
Archive data securely and compliant.
Discover easy archive
easy
invoice
Digitally verify and approve invoices.
Discover easy invoice
Newsroom
Media Library
Glossary
Contact us
+1 267 313 57-80
info[at]easy-software.com
Newsletter
We will keep you regularly up to date. Subscribe to our newsletter and find out everything you need to know about the digitization of business processes. The topics will be prepared for you in a tailor-made and varied way.
Newsletter subscription
Solutions
easy
archive
easy
invoice
easy
contract
easy
hr
easy
DMS
Application areas
Archiving
Document Capture
Procurement Processes
Digital Personnel File
Digital Signature
Document Management
Incoming Invoices
Electronic File
Contract Management
Workflows
Resources
Support
Academy
Managed Services
Blog
Media Resources
Events
Customer Stories
Glossary
About easy
Company
Press & News
Investor Relations
Find partners
Application areas
Archiving
Document Capture
Procurement Processes
Digital Personnel File
Digital Signature
Document Management
Incoming invoices
Electronic File
Contract Management
Workflows
Resources
Support
Academy
Managed Services
Blog
Media Resources
Events
Customer Stories
Glossary
About easy
Company
Press & News
Investor Relations
Careers
Find partners
Social
easy
is a
conrizon
brand offering established software products for compliant archiving, invoice processing, contract management, and human resources management. The right solution for every challenge, industry and company size.
www.conrizon.com
Imprint
General terms and conditions
Disclaimer
Privacy
Privacy Settings
Search for:', '310c06036e6c632e97f25421fbb046b82ba98fe364cfc42ce611bf5d85f687e9', '{"url": "https://easy-software.com/en/glossary/data-protection-the-cornerstones-of-digital-sovereignty-gdpr-gobd/", "title": "Data Protection (GDPR & GoBD) – Explanation – Glossary", "accessed_at": "2026-01-16T21:13:28.199393+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:13:28.200511+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (16, 'website', 'https://www.fiskaly.com/blog/understanding-gobd-compliant-archiving', 'GoBD: Understanding the requirements for proper digital bookkeeping', NULL, 'GoBD explained: Requirements for bookkeeping and archiving in Germany
fiskaly.
ENG
Enter your search term
Open menu
ENG
GoBD:
Understanding
the
requirements
for
proper
digital
bookkeeping
Victoria Waba
Content Marketing Manager
5/27/2025
3 min read
GoBD is a central regulatory framework for digital accounting and the retention of tax-relevant data in Germany. The revised version of the GoBD guidelines stipulates that certain requirements for the archiving of receipts must be met. In combination with the Cash Security Ordinance (Kassensicherungsverordnung), they ensure that digital business processes comply with the requirements of the tax authorities. For companies, this means: They must carefully document, regularly review, and legally structure their IT systems and internal processes—to avoid fines, estimates, or audit risks. Let''s take a detailed look at the requirements.
What are the GoBD?
The
GoBD
(short for “Grundsätze zur ordnungsgemäßen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff” – Principles for the Proper Keeping and Retention of Books, Records and Documents in Electronic Form as well as for Data Access) are an administrative regulation and a central component of the tax system in Germany.
The definition of the GoBD emphasizes their role in digital accounting, as well as the documentation requirements to ensure compliance with the tax office. They were introduced by the Federal Ministry of Finance (BMF) and specify the requirements arising from the German Fiscal Code (AO) and other tax regulations.
The
GoBD define in detail
how companies must capture, store, retain, and provide tax-relevant data to the tax authorities in the event of an audit. The aim is to ensure transparency, traceability, and immutability of digital records.
What are the legal principles of the GoBD?
The GoBD are based on various legal foundations, especially
§§ 146 and 147 of the Fiscal Code (AO)
. These paragraphs regulate the obligation to keep books and the retention of tax-relevant documents. The GoBD principles specify these regulations concerning digital processes and systems and apply to all companies, self-employed persons, and freelancers in Germany—regardless of size or industry.
Key requirements of the GoBD include:
Traceability and verifiability
of all entries and records
Immutability
of data after its recording
Timely recording
of business transactions
Proper retention
of digital documents for 10 years
Documentation
of the IT systems and processes used
Access rights
for the tax authorities
Recording obligations
according to the GoBD to ensure unchangeable and audit-proof documentation of receipts and bookings
The current version of the GoBD came into effect on January 1, 2020.
For which documents do the GoBD regulations apply?
The
GoBD regulations apply to all tax-relevant documents
, regardless of whether they are in
electronic or paper form
,especially when they are
digitally created, received, processed, or archived
. The goal is to ensure the properness of accounting and complete traceability for the tax office.
The most important document types covered by the GoBD include:
Records and business transactions (cash reports, individual cash data, outgoing and incoming invoices)
Electronic documents and receipts (
e-invoices
in structured electronic data format, system logs, TSS logs)
Accounting documents
Commercial and business correspondence
System and process documentation
Is GoBD-compliant archiving mandatory?
Yes, the
GoBD is mandatory
—however, not in the form of an independent law, but as an
administrative regulation from the Federal Ministry of Finance (BMF)
. Nevertheless, the requirements are binding, as they specify the legal obligations of the
Fiscal Code (AO)
.
The GoBD are based on the following paragraphs of the Fiscal Code:
§ 146 AO – Rules for bookkeeping
§ 147 AO – Retention of documents
This means:
Anyone who is legally obliged to keep books or voluntarily keeps books and records (e.g., self-employed persons, traders, freelancers) must comply with the GoBD. Even in purely digital or partially digital accounting, the GoBD requirements apply without restriction.
GoBD and their role in relation to KassenSichV
In addition to the Cash Register Act and the KassenSichV, the GoBD are relevant for cash register operators. The guidelines published by the Federal Ministry of Finance in 2014 regulate the proper handling and retention of electronic data. Entrepreneurs themselves are responsible for compliance with the GoBD, even if they entrust their accounting documents to a tax advisor. Although they are an administrative regulation and not a law, they have legal significance.
The GoBD define key requirements such as the immutability of booking data and the traceability of business transactions—requirements also enshrined in the KassenSichV and mandatory for companies. Together, the KassenSichV and the GoBD ensure that companies not only meet the technical requirements for protection against manipulation but also fulfill general accounting obligations properly. This contributes to legal certainty and enables the tax authorities to efficiently monitor compliance with tax regulations.
Requirements for the retention of cash register data
In Germany, there has so far been no general obligation to use electronic cash registers. Companies can choose between electronic registers and open cash drawers. Electronic cash registers must be equipped with a
Technical Security System (TSS)
. Open cash drawers are exempt from this requirement but require stricter documentation under the GoBD. However, the coalition agreement between CDU, CSU, and SPD provides for the introduction of a
cash register obligation for companies
with annual sales exceeding 100,000 euros starting January 1, 2027.
Cash register data must generally be
retained for ten years
. This retention period applies to all tax-relevant documents, including cash register data, in accordance with the legal requirements of the German Commercial Code (HGB) and the Fiscal Code (AO).
They must be
stored completely and immutably
. Every change to the cash register system and every subsequent change to the data must be logged and also retained.
In addition, the data must always be
available, immediately readable, and machine-evaluable
. Deficiencies in data retention can lead to significant tax back payments and penalties.
9/19/2025
7 min read
Navigating fiscalization regulations in Europe
Fiscalization ensures secure and transparent tax compliance, but rules vary across Europe. Explore the fiscalization frameworks of Germany, Austria, Spain, Italy, and France, including certified systems, electronic receipts, and real-time reporting. Discover how to stay compliant and simplify tax processes in key European markets.
Read post
12/5/2024
5 min read
Fiscalization in Hospitality: LINA and fiskaly Join Forces
By integrating fiskaly’s Cloud-TSS into the LINA TeamCloud platform, Gastro-MIS offers an innovative fiscalization solution for gastronomy. This cloud-based technical security system complies with all legal requirements, eliminates the need for hardware, and is easy to implement – future-proof, flexible, and 100% compliant.
Read post
3/22/2024
5 min read
Head-on Solutions: Cloud-based KassenSichV compliance with SIGN DE
studiolution, the web-based POS software from Head-on Solutions GmbH, fiscalizes with fiskaly SIGN DE and thus enables a simple end customer experience. With a Germany-wide user group, studiolution aims to offer its customers a compliant POS system that meets all legal requirements.
Read post
fiskaly.
Receipts made simple
Solutions
KassenSichV
DSFINVK DE
RKSV
TicketBAI
Veri*factu
Fiscal archiving
Digital receipt
Services
SIGN DE
DSFINVK DE
SUBMIT DE
SIGN AT
SIGN ES
SIGN IT
SIGN FR
SAFE & SAFE flex
RECEIPT
Fiskalcheck
Resources
Documentation
Dashboard
Support
Newsletter
Press & Media
Certificates
Glossary
Company
Team
About
Blog
Culture
Jobs
Partners
Research
Legal
Cookies Settings
Legal Notice
Privacy Policy
Terms
Trust Center
©
2026
fiskaly GmbH.
All rights reserved.', '0c519365568d2fc6f9c668623a62c68c9f5efec5cd64defcc8d0c806de4040b4', '{"url": "https://www.fiskaly.com/blog/understanding-gobd-compliant-archiving", "title": "GoBD explained: Requirements for bookkeeping and archiving in Germany", "accessed_at": "2026-01-16T21:13:28.762927+00:00", "status_code": 200, "content_type": "text/html; charset=utf-8"}', '2026-01-16T21:13:28.763947+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (17, 'website', 'https://www.bdo.de/en-gb/services-en/audit-assurance-en/it-controls-assurance/it-assurance/gobd-compliance', 'GoBD Compliance Audits - BDO Germany', NULL, 'GoBD Compliance Audits - BDO Germany - BDO
Offices
News
Global Portal
de
German
en
English
Contact
Germany
BDO
Services
Audit & Assurance
IT & Controls Assurance
IT Assurance
GoBD Compliance
GoBD Compliance
Insights
Contact
The digital tax audit complements the existing form of the tax audit. For many years now, all companies that use electronic data processing have been obliged to maintain corresponding data in digital form. The tax authorities have specified the requirements of the German Fiscal Code (AO) in more detail in the "Principles for the proper management and storage of books, records and documents in electronic form and for data access" (GoBD, see BMF letter dated November 28, 2019). These relate not only to the audit-proof storage of documents, but also to the entire processing chain from the creation and recording of the business transaction, through its processing in the business processes (and IT applications), to the tax balance sheet, and therefore not only affect
financial accounting
, but also upstream systems. A documentation of these processes must be created, which should also include aspects such as authorization concepts, internal control system and general processes for IT operations.
The
IT & Controls Assurance
department supports you in evaluating the conformity of your processes, systems and procedural documentation.
The objective of the digital tax audit is the structured analysis of tax-relevant company data instead of the previously common single document audits, especially to find tax loopholes more easily. Employees of the tax authorities must be granted access to tax-relevant electronic company data during an external audit. In order to guarantee this access, rules have been defined in the GoBD.
Data access can be granted in three different ways:
direct access: Z1
The employee of the tax authority checks himself in the company, whereby all necessary data must be made accessible to him. For this purpose, a read-only access must be set up. The auditor cannot be held liable for any damage caused by misuse, so we strongly recommend read-only access.
indirect access: Z2
The auditor comes to the company to have the relevant data shown to him. The enterprise concerned must evaluate the tax-relevant data itself by machine according to the specifications of the tester, in order to then allow the tester read access to the prepared data.
data medium provision: Z3
The inspector requests the relevant data to be checked by the authority. The company concerned must submit all data in digital form and in a format that can be evaluated by machine.
The digital tax audit has legal and organizational effects on companies: In addition to the audit-proof documentation and archiving of all relevant data, possibly also the acquisition of suitable hardware and software that enables "machine readability" and "random access".
In this context, "audit-proof" means that once data has been created, it can no longer be changed (unnoticed) afterwards. "Machine readability" means that the data is available in a format that enables structured evaluation. Important links must be documented. Archiving in the form of e.g. PDF documents or in document archives is therefore by no means sufficient.
The "IDEA" software used by the auditors supports numerous financial accounting, database and text formats. "Optional access" means independence from the programs that generated the data, i.e. in the concrete case again mainly the choice of a format that can be read by IDEA.
Target group:
all companies that use business software
all companies that originally exchange electronic tax-relevant data, i.e. data that is received electronically, e.g. by e-mail or as an electronic invoice, also process it electronically
all companies in which electronic data is generated by the computer system itself, i.e. accounting records of the financial accounting etc.
The term of the tax-relevant data is unfortunately not clearly defined. Generally, it applies however that the extent of the exterior examination did not change, i.e. the same data as before as tax-relevant are considered, thus e.g.:
Financial accounting
Asset Accounting
Wage & Salary
Order processing / Ordering
Warehouse / Inventory
In addition to the books, inventories, annual financial statements and accounting vouchers listed in § 147 para. 1 AO as examples, this includes in particular all data from financial, payroll and asset accounting.
Tax-relevant data in the sense of § 147 para. 1 No. 5 AO can also be generated, however, e.g. in the merchandise and materials management system, in customer relationship management, in invoicing, in electronic banking, in the cash book, in time recording and travel expense accounting. If, for example, a company runs its own system for travel expense accounting and only the totals postings are transferred to
payroll accounting
, then the travel expense accounting system would also be relevant for payroll tax.
Similarly, all calculation bases created electronically (e.g. as an Excel file) must be opened for data access if only the calculation results have been entered into the accounting. For example, price calculations may be tax-relevant if they were used to determine the manufacturing costs or as a benchmark for intra-group transfer prices. For this reason, no module or subsystem of the company''s own IT system may be excluded from the identification of tax-relevant data.
It is problematic that tax-relevant data can be available in different formats, e.g. invoices by e-mail, EDIFACT data, etc. All this data must be archived and made available to an auditor.
Rules for storage:
Data must be retained for six or ten years, depending on the type of company, regardless of any system changes in hardware and software
The data must be available at all times, including from external service providers such as tax consultants, DATEV, etc.
The data must be made readable immediately
The data are by machine evaluable (via IDEA)
To Do list:
Check your business software to see if it can generate audit-proof data.
Check all areas of your company (e.g. EDI, e-mail, web, online banking) to see whether tax-relevant data is generated there.
Perform regular backups of all tax-relevant data.
Define company work instructions for deleting or changing data in compliance with regulations.
Avoid private data on the company computer.
Play through a tax audit once and prepare for the new focal points of the audit: Complete audit, instead of random checks.
Talk to us!
Contact us!
Karsten Thomas
Partner, IT & Controls Assurance
View bio
Frank  Gerber
German Public Auditor, Certified Tax Advisor, Partner, IT & Controls Assurance
View bio
Karl-Heinz Tebbe
Partner, IT & Performance Advisory
View bio
Insights: GoBD Compliance
Contact
Offices
Imprint
Sitemap
Our Whistleblower system
Opens in a new window/tab
Cookie settings
Data protection statement
Legal Information
Engagement Terms & Conditions
BCRS
Opens in a new window/tab
Opens in a new window/tab
Opens in a new window/tab
BDO AG Wirtschaftsprüfungsgesellschaft, a German company limited by shares, is a member of BDO International Limited, a UK company limited by guarantee, and forms part of the international BDO network of independent member firms. BDO is the brand name for the BDO network and for each of the BDO Member Firms. ​ © 2026', 'bcc5ea72aaff68f4bcdf3613413d75b1f0ad02ae8739f4ad162e9c42d4c72a7d', '{"url": "https://www.bdo.de/en-gb/services-en/audit-assurance-en/it-controls-assurance/it-assurance/gobd-compliance", "title": "GoBD Compliance Audits - BDO Germany - BDO", "accessed_at": "2026-01-16T21:13:31.596772+00:00", "status_code": 200, "content_type": "text/html; charset=utf-8"}', '2026-01-16T21:13:31.597510+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (18, 'website', 'https://www.pwc.de/en/steuern/we-will-make-sure-you-are-gobd-compliant.html', 'We will make sure you are GoBD-compliant - pwc.de', NULL, 'GoBD Compliance - PwC
Skip to content
Skip to footer
Expertise
Industry Sectors
Store
About us
Locations
More
Search
Back (see screen at page 12)
Expertise
Expertise
Cloud & Digital
Corporate Innovation
Customer Transformation
Cyber Security
Deal Advisory
ERP Transformation
Finance Transformation
Forensic Services
Legal Advice
Operations Transformation
Performance & Restructuring
Risk & Regulatory
Sustainability
Taxes & Law
Back (see screen at page 12)
Expertise
Cloud & Digital
Cloud Transformation
Generative AI
Open Source Consulting
Quantum Computing
Back (see screen at page 12)
Expertise
Corporate Innovation
Back (see screen at page 12)
Expertise
Customer Transformation
Back (see screen at page 12)
Expertise
Cyber Security
Back (see screen at page 12)
Expertise
Deal Advisory
Back (see screen at page 12)
Expertise
ERP Transformation
Back (see screen at page 12)
Expertise
Finance Transformation
Back (see screen at page 12)
Expertise
Forensic Services
Back (see screen at page 12)
Expertise
Legal Advice
Back (see screen at page 12)
Expertise
Operations Transformation
Back (see screen at page 12)
Expertise
Performance & Restructuring
Back (see screen at page 12)
Expertise
Risk & Regulatory
Back (see screen at page 12)
Expertise
Sustainability
Back (see screen at page 12)
Expertise
Taxes & Law
Digital Services Tax & Legal
Indirect Taxes
Tax Advice for Companies
Tax Advice for Private Clients
Transfer Pricing Consulting
Featured
Managed Services
Strategic Alliances
Engineering Future Relevance – with Data & AI
Back (see screen at page 12)
Industry Sectors
Industry Sectors
Automotive
Energy
Financial Services
Health Industries
Industrial Products
International Markets
Private Equity
Real Estate
Retail and Consumer
Startup & Scaleup Consulting
Supervisory Board
Technology, Media and Telecommunications
Transport and Logistics
Back (see screen at page 12)
Industry Sectors
Automotive
Back (see screen at page 12)
Industry Sectors
Energy
Back (see screen at page 12)
Industry Sectors
Financial Services
Back (see screen at page 12)
Industry Sectors
Health Industries
Back (see screen at page 12)
Industry Sectors
Industrial Products
Back (see screen at page 12)
Industry Sectors
International Markets
Back (see screen at page 12)
Industry Sectors
Private Equity
Back (see screen at page 12)
Industry Sectors
Real Estate
Back (see screen at page 12)
Industry Sectors
Retail and Consumer
Back (see screen at page 12)
Industry Sectors
Startup & Scaleup Consulting
Back (see screen at page 12)
Industry Sectors
Supervisory Board
Back (see screen at page 12)
Industry Sectors
Technology, Media and Telecommunications
Back (see screen at page 12)
Industry Sectors
Transport and Logistics
Back (see screen at page 12)
Store
Store
Back (see screen at page 12)
About us
About us
PwC Germany
Company Profile
The Territory Leadership Team
Organisational Structure
Analyst Relations
Newsletters
Ethics and Compliance: Information and Helpline
Back (see screen at page 12)
About us
PwC Germany
Back (see screen at page 12)
About us
Company Profile
Back (see screen at page 12)
About us
The Territory Leadership Team
Back (see screen at page 12)
About us
Organisational Structure
Back (see screen at page 12)
About us
Analyst Relations
Back (see screen at page 12)
About us
Newsletters
Back (see screen at page 12)
About us
Ethics and Compliance: Information and Helpline
Back (see screen at page 12)
Locations
Locations
Berlin
Bielefeld
Bremen
Cologne
Düsseldorf
Erfurt
Essen
Frankfurt
Hamburg
Hanover
Kassel
Kiel
Leipzig
Mannheim
Munich
Nuremberg
Osnabrück
Saarbrücken
Schwerin
Stuttgart
Back (see screen at page 12)
Locations
Berlin
Back (see screen at page 12)
Locations
Bielefeld
Back (see screen at page 12)
Locations
Bremen
Back (see screen at page 12)
Locations
Cologne
Back (see screen at page 12)
Locations
Düsseldorf
Back (see screen at page 12)
Locations
Erfurt
Back (see screen at page 12)
Locations
Essen
Back (see screen at page 12)
Locations
Frankfurt
Back (see screen at page 12)
Locations
Hamburg
Back (see screen at page 12)
Locations
Hanover
Back (see screen at page 12)
Locations
Kassel
Back (see screen at page 12)
Locations
Kiel
Back (see screen at page 12)
Locations
Leipzig
Back (see screen at page 12)
Locations
Mannheim
Back (see screen at page 12)
Locations
Munich
Back (see screen at page 12)
Locations
Nuremberg
Back (see screen at page 12)
Locations
Osnabrück
Back (see screen at page 12)
Locations
Saarbrücken
Back (see screen at page 12)
Locations
Schwerin
Back (see screen at page 12)
Locations
Stuttgart
Loading Results
No Match Found
View All Results
We will make sure you are GoBD-compliant – GoBD as an integral component of a tax CMS
Copy link
Link copied to clipboard
11 August, 2020
In November 2019, the German Federal Ministry of Finance (Bundesministerium für Finanzen – BMF) published the revised version of the “Principles for the Proper Management and Storage of Books, Records and Documents in Electronic Form as well as Data Access” (Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff – GoBD). This replaces the original BMF Circular regarding the GoBD from 2014. Because the GoBD are not a law but rather an interpretation by the tax authorities, implementing them is not directly mandatory for the taxpayer.
In many cases, however, without implementation, taxpayers will find it difficult to prove that their accounting is compliant if errors are found in their tax declaration. This is because a vital prerequisite for proving such compliance is the transparent presentation of how the company processes the data of a transaction using its IT systems – from its initial recognition through to the tax declaration. The requirements of the GoBD therefore constitute an integral component of an effective
tax compliance management system (tax CMS)
, which in turn serves as a vital instrument for reducing tax-related liability risks.
Your expert for questions
Matthias Walz
Partner, PwC Germany
Tel.: +49 711 25034-3203
E-Mail
Risks arising from tax audits
Companies that do not fulfill their GoBD requirements risk facing trouble even in the course of tax audits. They must expect that tax auditors will have doubts about the formal correctness of their accounting. If incorrect information is found in the tax declaration in addition to this, unpleasant consequences could ensue – ranging from a major reassessment of the company''s tax base to administrative offense or even criminal tax proceedings.
According to the information we have gathered, the Federal Central Tax Office (Bundeszentralamt für Steuern – BZSt) has instructed tax auditors throughout Germany to assess companies'' adherence to GoBD requirements more intensively in future. Many of our clients have already received notifications of tax audits that have included requests to provide GoBD documentation.
Our PwC experts are thoroughly familiar with the requirements of the BMF Circular and have extensive experience with the compliance requirements of the tax authorities. We can support you in the implementation and documentation of all GoBD requirements with specialist, tailored solutions so that you can look towards your next tax audit with peace of mind. As part of this process, it is also possible – if requested – to identify opportunities for automation and digitalization in order to exploit such potential.
Our GoBD solutions for you
Analysis
Implementation support
Assessment
Health check
In our health check, we thoroughly assess your company to identify potential risks and challenges that may pose an obstacle to the fulfillment of GoBD requirements. We issue a final report that provides you with an overview of the topics that must be worked on in order to implement the GoBD requirements successfully.
Gap analysis
In our gap analysis, we carry out a detailed examination of the existing measures, processes and documentation for the fulfillment of the requirements of the BMF Circular regarding the GoBD. In doing so, we not only identify weaknesses, risks and opportunities for optimization, we also determine what existing work results and documents you can repurpose for the implementation of GoBD requirements so that you can avoid any unnecessary additional workload.
GoBD concept
In our GoBD concept solution, we collaborate with you in finding a way to implement the tax authorities'' requirements for your company in a sensible manner. If you have already developed a GoBD concept, we will be pleased to assess it with regard to its completeness and feasibility. This will enable you to benefit from our experience and specialist expertise.
Storage obligations
We have noticed that violations of retention obligations are being penalized with increasing frequency in the course of tax audits. We will assist you in identifying documents subject to retention obligations, in the implementation of the requirements of Section 147 of the German Fiscal Code (Abgabenordnung – AO) and with the requirements of the BMF Circular regarding the GoBD. This means that you will be able to look towards your next tax audit with peace of mind.
IT tools and technologies
The rising pace of digitalization means that more and more manual processes are being supported or even replaced by IT tools and new technologies. The tax risks associated with these transformations are often underestimated. In order to minimize tax risks, we will assist you in fulfilling the requirements of the BMF Circular regarding the GoBD.
We will be pleased to assist you by providing advice about the identification, selection and implementation of such IT tools and technologies.
Archive & document management systems
The tax authorities have very high requirements with regard to GoBD-compliant archive and document management systems. We will assist you in meeting the requirements of the BMF Circular regarding the GoBD in relation to the authenticity, integrity, availability and authorization of digitally archived data.
Record-keeping obligations
With a view to the record-keeping obligations, we will assist you in designing GoBD-compliant processes for electronic accounting, extending from daybook records and journal entries to general ledger records. Our expert team will ensure that the processes implemented guarantee complete, correct, punctual, orderly and efficient accounting for every single transaction.
Identification & description of IT controls
We will assist you in assessing your existing IT systems for risks and use this assessment to determine what controls are needed in your case. We thus ensure that the controls that are recommended and required on the basis of the BMF Circular regarding the GoBD are in place. In this context, we can draw on a pool of standardized controls – or we can develop tailored controls for your IT landscape.
Data access for the tax authorities
We will assist you in determining what access to data the tax authorities require for each data processing system and in finding a way to implement these access requirements. We will also advise you about how to deal with (productive) systems that are no longer active.
Identifying tax-relevant IT systems
With the large number of IT systems used for a vast range of activities, our clients often have difficulty in determining which IT systems are relevant to the GoBD. We will assist you in singling out the relevant IT systems.
Authorization concepts
We will assist you in drawing up and implementing IT authorization concepts that will ensure adherence to the organizational segregation of duties and compliance with GoBD-specific requirements.
Procedure documentation
We will assist you in preparing GoBD-compliant procedure documentation for individual data processing systems and for all GoBD-relevant parts of your IT landscape.
Readiness check
In our readiness check, we will determine the current maturity level of your GoBD implementation. You will then receive our assessment of its current status and whether certification should be considered at this time. Furthermore, if needed, we will be able to assist you with the implementation as an independent third party.
Assessment of appropriateness
Using our assessment of appropriateness, we determine whether the measures and principals that you have described are suitable for ensuring conformity with the GoBD. You will still be able to remedy findings from this assessment and this will be taken into account in our final audit report. If requested, we can also certify that the assessment of appropriateness was successful.
Assessment of effectiveness
In the assessment of effectiveness, we go beyond the assessment of appropriateness to determine the extent to which the measures and principles were actually effective for a defined period and ask: Are the documented processes, measures and controls lived out? As with the assessment of appropriateness, it is also possible for you to remedy any findings from this assessment and to receive certification if desired.
The requirements of the tax authorities
General requirements
Voucher system (voucher function)
Record-keeping obligations
Internal control system
Data security
Unalterability and logging
Storage
Procedure documentation
Data access
General requirements
[margin no. 22-60]
The GoBD stipulate that the documents that are present in paper form or electronic form must be comprehensible [margin no. 30], verifiable [margin no. 145], complete and without gaps [margin no. 36] for all transactions. These requirements also apply to documents that are important for understanding and reviewing records that are legally required for tax purposes.
Voucher system (voucher function)
[margin no. 61-81]
The voucher system is a basic prerequisite for the substantiation of proper accounting and it is therefore of material importance. The BMF Circular breaks down this requirement as follows:
Secure storage of vouchers
[margin no. 67]
Taxpayers must store vouchers in a manner that secures them against potential loss promptly after their receipt or creation. To this end, BMF Circular provides for organizational measures (e.g. continuously filing paper vouchers in folders or issuing voucher numbers including subsequent recording as an image). In the case of electronic vouchers, the issuing of voucher numbers can also be automated.
Attribution of the individual voucher to the associated daybook record or entry
[margin no. 71]
Taxpayers must ensure that all vouchers can be unambiguously attributed to a daybook record or entry. This must be done by providing them with unambiguous attribution and identification features. These features must be selected such that any transaction is unambiguously comprehensible and provable within an appropriate period of time.
Preparing vouchers appropriately for recording
[margin no. 75]
In order for vouchers to be prepared appropriately for recording, they must contain the minimum details listed under margin no. 77. This includes, in particular, an unambiguous voucher number, the voucher date, the amount and details of the volume and value that result in the amount to be booked, as well as a sufficient description of the transaction.
The procedure for the receipt, recording, processing, issuance and retention of electronic vouchers has to be documented in the procedure documentation.
Important new developments:
The new BMF Circular explicitly permits the recording of invoices in digital form, e.g. using smartphones or other mobile devices. In addition, the BMF has revised the requirements for the recording of invoices abroad. By doing so, the ministry has cleared the way for the use of digital tools and technologies. These requirements are specified in the BMF Circular under "Storage" ("Aufbewahrung").
Record-keeping obligations
[margin no. 82-99]
The regulatory requirements for accounting and records are essentially derived from the AO (Section 146).
The BMF Circular provides details about these requirements with regard to
recording in the daybook records [margin no. 85],
digital daybook records [margin no. 87],
entries in the journal (journal function) [margin no. 90], as well as
records of transactions in the general ledger [margin no. 95].
Each record must relate to one voucher and be made individually, completely, correctly, on time and in an orderly manner [margin no. 82].
In cases of double-entry bookkeeping, it must additionally be possible to present all transactions in chronological order (journal function) and structured according to subject matter (account function) [margin no. 83].
In this context, it must be ensured that, once recorded, digital records cannot be altered without authorization or without proof of the original version of the records (revision-proof retention) [margin no. 88]. Furthermore, companies must save all necessary table data and their history records.
Challenges arise where companies use more than one tax-relevant IT system. For the transfer of data from IT applications, it must be ensured that aggregate entries are carried over and that the general and sub-ledgers are reconciled. These controls must be described in the GoBD procedure documentation.
Internal control system
[margin no. 100-102]
In order to ensure that the taxpayer adheres to the regulatory requirements pursuant to the AO, the BMF Circular calls for an internal control system (ICS) [margin no. 100]. The corresponding controls should be set up, performed and logged within the context of the ICS. The ICS should ensure that companies fulfill the general requirements as well as the requirements regarding vouchers and record-keeping obligations. In this regard, the BMF Circular does not explicitly govern how an ICS should be configured but lists examples of necessary measures:
access and authorization controls,
segregation of duties,
recording controls,
reconciliation controls for data entry,
processing controls, as well as
protective measures against intentional and unintentional falsification.
The same can be said about the application decree pursuant to Section 153 AO, which also calls for an "internal control system for the fulfillment of tax obligations" and where the BMF does not provide any detailed instructions on system design either. Additional details about the requirements for the ICS for the purposes defined by the tax authorities are provided in the relevant Practical Note (IDW PH 1/2016) of the Institute of Public Auditors in Germany (Institut der Wirtschaftsprüfer in Deutschland – IDW). Under the heading of "Tax Compliance Management System" (tax CMS), this document describes the framework for a tax ICS and its integration into a company-wide control system.
How the tax ICS is implemented in accordance with the GoBD depends on the complexity of the processes, the applicable tax requirements and the technological diversity within the company. This topic poses major challenges for those responsible. How extensive controls need to be depends, in particular, on the complexity of the ERP system – e.g. how many interfaces exist and whether the company uses standard applications, user-specific software or in-house developments. Furthermore, the performance of controls has to be documented as part of the ICS and the ICS must be kept up to date.
In general, it can be stated that companies must ensure that all risks resulting from data processing are covered by corresponding controls. This includes both IT-based data processing and the exchange of data via system interfaces. It is therefore strongly recommended that employees with experience of ICS documentation processes are involved in the GoBD documentation process.
The description of the internal control system is a component of the procedure documentation that is also required [margin no. 151].
Data security
[margin no. 103-106]
One of the requirements of the tax authorities is data security. Companies must therefore ensure that data is protected from loss, unauthorized access and manipulation. According to the BMF''s interpretation, accounting no longer fulfills the formal requirements if data is insufficiently protected. However, the BMF Circular provides taxpayers with hardly any indications of how to achieve sufficient data security in practice.
The BMF describes key features of data security under the headings of "Unalterability and logging" as well as "Storage".
Unalterability and logging
[margin no. 107-112]
The unalterability and logging of accounting entries and/or records is already statutorily regulated under the AO. In the accounting standard IDW AcS FAIT 1 "Generally Accepted Accounting Principles for the Use of Information Technology" (DW RS FAIT 1" Grundsätze ordnungsmäßiger Buchführung bei Einsatz von Informationstechnologie"), the Institute of Public Auditors in Germany (Institut der Wirtschaftsprüfer in Deutschland – IDW) has provided greater specificity regarding the requirements in this area.
The requirements of the BMF nevertheless go beyond the statutory provisions. For example, the ministry considers it a GoBD violation if changes in accounting entries or records are even possible. In this context, the BMF additionally states a position on data management systems (DMS) and explicitly points out that such systems are only GoBD-compliant if companies take suitable measures to ensure unalterability and logging. Examples given by the BMF here include:
Hardware-based measures (e.g. unalterable and tamper-proof data storage media)
Software-based measures (e.g. back-ups, blocking, permanent records, deletion flags, automatic logging, history records, versioning)
Organizational measures e.g. (access authorization concept and user management)
Storage
[margin no. 113-144]
The storage obligation for tax purposes is very broadly defined under the AO. It extends to all documents and data that are related to the preparation, execution, conclusion or cancellation of business transactions. In the absence of a precise definition of relevant documents and data, the task of classification is left to the taxpayer. Orientation is provided by the GoBD provisions on machine analyzability [margin no. 125], electronic storage [margin no. 130], image recording of paper documents [margin no. 136] as well as the outsourcing of data from the productive system and system changes [margin no. 142].
The BMF calls for the comprehensive documentation of the receipt, archiving, (where applicable) conversion and further processing of documents and data subject to the storage requirement. In addition, the procedure documentation must be stored with all change versions. Furthermore, all archived documents and data are to be stored in an orderly manner.
This relates to all tax-related systems in which data is received, processed or comes into existence. In order to sort them, documents and data may be changed, e.g. by means of indexing or booking references. Nevertheless, changes have to be logged. Apart from this, when archiving documents and data, taxpayers must ensure that machine analysis is possible for the entire duration of the storage obligation.
Important new developments:
In the new BMF Circular, the ministry has, in particular, significantly simplified the provisions on electronic storage and the electronic recording of paper documents. For instance, recording paper vouchers in image form will be possible in a number of ways in future (e.g. using smartphones and doing so abroad if the vouchers came into existence there/were received and immediately recorded there). In cases where an approved relocation of accounting abroad has taken place (e.g. to a shared service center), the image-based recording of received original paper vouchers in this country will no longer be objectionable either, provided that such records are created promptly for transmission abroad.
In addition, under certain circumstances [margin no. 135], it is now sufficient if companies archive exclusively documentation that has been converted into their own in-house formats. In such cases, the obligation to archive the original files no longer applies.
Procedure documentation
[margin no. 145-157]
GoBD procedure documentation is becoming more and more important. For months we have been seeing how the tax authorities are increasingly requesting that companies provide their procedure documentation within the scope of tax audits. The BMF calls for each tax-relevant IT system to have procedure documentation that describes its technical procedure and processing and should contain the following components as standard:
General description
User documentation
Technical system documentation
Operating documentation
For all system and procedure changes, versioning is to be carried out and a complete alteration history is to be maintained indicating the content and time of changes.
The specific configuration of the procedure documentation depends on the complexity and diversification of the business activities concerned as well as organizational structures and the data-processing systems used [margin no. 151].
Important new developments:
The revised BMF Circular provides greater detail on the obligation to maintain complete and comprehensible documentation of vouchers (from generation to tax declaration). The requirements regarding the historical traceability of changes to the procedure documentation have been simplified. A template for the procedure documentation that takes the new BMF Circular into account can be obtained from the relevant chambers and associations.
Data access
[margin no. 158-178]
The tax authorities have the right to access data in order to examine the documentation that is subject to the AO storage requirement and has been prepared using a data-processing system [margin no. 158]. The BMF formulates the three possible forms of data access as follows:
Direct access
The tax authority has the right to access the data-processing system directly itself in such a way that, by way of read-only access, it inspects the data subject to the recording and storage requirement and uses the hardware and software used by the taxpayer or commissioned third party for its examination of the saved data, including the respective metadata, master data and transaction data as well as the relevant links (e.g. between the tables of a relational database).
Indirect access
The tax authority can also require that the taxpayer, acting on its behalf, conduct a machine analysis of the data subject to the recording and storage obligation in accordance with its requirements or have a commissioned third party conduct a machine analysis of such data in order to allow for read-only access thereafter. It is only possible to require a machine analysis that uses the analysis capabilities that exist within the data-processing system of the taxpayer or of the commissioned third party.
Handing over data media
The tax authority can furthermore require that the data subject to the recording and storage requirement, including the respective metadata, master data and transaction data as well as internal and external links (e.g. between the tables of a relational database), and electronic documents and documentation be handed over to it on a machine-readable and analyzable data medium for analysis. The tax authority is not entitled to download data from the data-processing system itself or to make copies of available data backups.
During the statutory storage period, companies must ensure that the three specified forms of access are continuously possible.
Important new developments:
Previously, the form of data access was at the discretion of the tax authorities. This meant that companies could face problems because they had to maintain all three potential forms of access for inactive systems as well. Under the new BMF Circular, the tax authorities can no longer require the first and second form of access for inactive systems once the fifth calendar year subsequent to transition has expired. This will also mean, however, that the handing over of data media (third form of access) will have to be prepared and technically facilitated with the inclusion of all tax-relevant data. Recourse to the first or second forms of access is not possible in cases of doubt.
The BMF Circular now explicitly names inspections in the context of data access, in particular cash inspections including all upstream systems and subsystems.
Do you have questions?
Contact our expert
Related content
Digital Services Tax & Legal
We support you in your transformation process.
Contact us
Matthias Walz
Partner, PwC Germany
Tel: +49 170 8591849
Email
Rudolf Dirks
Senior Manager, Trust & Transparency Services, Risk Assurance Solutions, PwC Germany
Email
Christian Scheminski
Senior Manager Tax & Legal, Tax Reporting & Strategy, PwC Germany
Tel: +49 69 9585-6418
Email
Follow us
Contact us
Klaus Schmidt
Partner, Global Tax and Legal Managed Services / Alliances Leader, PwC Germany
Tel: +49 160 7032368
Email
Contact us
Hide
Offices
Contact us
© 2017
							
							 - 2026 PwC. All rights reserved. PwC refers to the PwC network and/or one or more of its member firms, each of which is a separate legal entity. Please see
www.pwc.com/structure
for further details.
Disclaimer
Imprint
Privacy policy
Digital Services Act
Terms of use
Cookie settings', 'e045220599773f6f99ea38a0eee1797bf9a9353c73800020aeb44b2e4045021f', '{"url": "https://www.pwc.de/en/steuern/we-will-make-sure-you-are-gobd-compliant.html", "title": "GoBD Compliance - PwC\r\n", "accessed_at": "2026-01-16T21:13:32.073191+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:13:32.074044+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (19, 'website', 'https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1539585682.html', 'NetSuite Applications Suite - Germany Audit Files: GoBD Data Extract', NULL, 'NetSuite Applications Suite - Germany Audit Files: GoBD Data Extract
Previous
Next
JavaScript must be enabled to correctly display this content
Country-Specific Features
Germany Help Topics
Germany-specific SuiteApps
Germany Localization
Germany Audit Files: GoBD Data Extract
Germany Audit Files: GoBD Data Extract
Note:
This topic is for NetSuite accounts that use the Country Tax Report page to generate Germany tax audit files (GOBD). If you''re using the Audit Files page, see
Germany GoBD Data Export
.
The German GrundsÃ¤tze zur ordnungsmÃ¤Ãigen FÃ¼hrung und Aufbewahrung von BÃ¼chern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff (GoBD) is a tax audit system mandated by German tax authorities. GoBD defines the regulatory principles and rules around the access, storage, and verification of financial data and the obligation of German taxpayers in case of a tax audit.
With Germany Localization, you can generate and export tax audit files in a format that complies with the GoBD standard.
The Germany Audit Files: GoBD Data Extract report supports Multi-Book Accounting and Adjustment-Only Book features. For more information, see
Multi-Book Accounting Overview
and
Adjustment-Only Books Overview
.
To generate the GoBD Data Extract, see
Generating Localized Country Tax Reports
.
For more information about the Multi-Book Accounting feature, see
MultiâBook Accounting and Adjustment-Only Book Support in Tax Reporting Framework
.
The GoBD Data Extract consists of 14 files. Click
Download
on the relevant line in the
Report Execution Log
table to start download. If the file download doesn''t start or if you receive incomplete files, check your browser settings and ensure that popup windows and multiple downloads features are enabled.
Note:
Please note that the Fixed Assets report file is only available if the Fixed Assets Management SuiteApp is installed in your account.
Important:
GoBD Data Extract files generated before updating to Germany Localization 1.07 will no longer be available for download after updating to Germany Localization 1.07. To make the files available for download, generate the reports again.
The following table describes each file from the GoBD Data Extract. Click on the report name in the File/Report column to view additional details and data sources for each report.
File/Report
Contents
Filename
DTD
Descriptive information about the files generated
The date is the last modification date of the GDPdU standard
gdpdu-01-08-2002.dtd
Index
Index to the files and their contents
index.xml
Company Data
Information about the company such as legal name, tax registration number, etc
company.txt
Chart of Accounts
Basic chart of accounts data without the account balances
accountslist.txt
Transaction Journal
All the general journal transaction details that includes tax code details
transaction_journal.txt
Sums and Balances List
Summary of the chart of accountsâ opening balances, movements, and ending balances
account_balances.txt
Account Sheets
All general ledger transactions
genledger.txt
Debtors
All transaction details for debtors
receivables.txt
Creditors
All transaction details for creditors
payables.txt
Receivables Master Data
List of customers with transactions for the period
receivables_master_data.txt
Payables Master Data
List of vendors with transactions for the period
payables_master_data.txt
Tax Codes
All tax codes and tax rates in the system
taxcodelist.txt
Annual VAT Report
All tax codes and corresponding postings (amounts) for the year
annualvat.txt
Fixed Assets
List of fixed assets for the period
fixedassets.txt
Company Data
The Company Data file (company.txt) contains company data such as legal name, tax registration number, address, phone number, and so on. It retrieves the information from the company record or subsidiary record.
The following table describes the fields and record data sources of the Company Data report.
Field
Source field
Audit_File_Version
The version number of the file.
Company_ID
Tax Registration Number for the Germany Nexus
Company_Name
Legal Name
Street
Address 1 and Address 2
Postal_Code
Zip
Region
State
Country
Country
Financial_Year
Selected Reporting Period
Currency
Base currency
Telephone
Phone
Fax
Fax
Accounting_Basis
Invoice Basis (Cash basis is currently not supported)
Chart of Accounts
The Chart of Accounts file (accountslist.txt) contains the basic chart of accounts data without the account balances. It retrieves the information from the companyâs or subsidiaryâs chart of accounts except non-posting accounts, non-posting account types, and statistical accounts. If you''re using Multi-Book Accounting, accounts restricted to accounting book other than the selected one won''t be shown.
The following table describes the fields and record data sources of the Chart of Accounts report.
Field
Source field
Account_ID
Account Number
Description
Account Description
Category
Account Category
Subcategory
Account Subcategory
Account_Type
Account Type
Transaction Journal
The Transaction Journal file (transaction_journal.txt) contains all the general journal transaction and tax code details. It retrieves the information from the transaction record and GL impact record. If you''re using Multi-Book Accounting, the contents are specific for the selected accounting book.
The following table describes the fields and record data sources of the Transaction Journal report.
Field
Source field
Internal_ID
Internal ID
Transaction_Type
Transaction type
Date
Date
Posting_Period
Posting Period
Document_Number
Transaction ID
Description
Line Description or Memo
Accounting_Book
Name of the Accounting Book
Note:
If applicable
Debit_Account
Debit Account
Debit_Amount
Debit Amount
Credit_Account
Credit Account
Credit_Amount
Credit Amount
VAT_Debit_Account
VAT Debit Account
VAT_Debit_Amount
VAT Debit Amount
VAT_Credit_Account
VAT Credit Account
VAT_Credit_Amount
VAT Credit Amount
Tax_Code
Tax Code
Currency
Base Currency of the Company or Subsidiary
Sums and Balances List
The Sum and Balances List file (account_balances.txt) contains a summary of the chart of accountsâ opening balances, movements, and ending balances, as well as from transaction records. If you''re using Multi-Book Accounting, the contents are specific for the selected accounting book.
The following table describes the fields and record data sources of the Sum and Balances List report.
Field
Source field
Account_ID
Chart of Accounts â ID
Account_Name
Chart of Accounts â Name
Accounting_Books
Name of the Accounting Book
Note:
If applicable
Last_Posting_Date
Transaction Date â Date of last transaction posted for the selected period.
Opening_Balance_Debit
Account Balances â Net balance of the account from previous periods. Can either be net debit or net credit.
If you want the value to be zero, use the Period End Journals feature for the selected reporting period. For more information, see
Period End Journal Entries
.
Opening_Balance_Credit
Account Balances â Net balance of the account from previous periods. Can either be net debit or net credit.
If you want the value to be zero, use the Period End Journals feature for the selected reporting period. For more information, see
Period End Journal Entries
.
Total_Debit
Account Balances â Total debit entries of the account for the selected period
Total_Credit
Account Balances â Total credit entries of the account for the selected period
YTD_Debit
Account Balances = Opening debit balance + total debit entries of the account
YTD_Credit
Account Balances = Opening credit balance + total credit entries of the account
YTD_Balance_Debit
Account Balances = (Opening debit balance â total credit entries of the account)
If YTD_Balance_Debit is lower than 0, displays 0.
YTD_Balance_Credit
Account Balances = (Opening credit balance â total debit entries of the account)
If YTD_Balance_Credit is lower than 0, displays 0.
Account Sheets
The Accounts Sheets file (genledger.txt) contains all general ledger transactions. It gathers information from the chart of accounts, account balances, transaction records, and GL impact records. If you''re using Multi-Book Accounting, the contents are specific for the selected accounting book.
The following table describes the fields and record data sources of the Accounts Sheets report.
Field
Source field
Account_Number
Chart of Accounts â Number
Account_Description
Chart of Accounts â Name
Accounting_Book
Name of the Accounting Book
Note:
If applicable
Transaction_Type
Transaction â Transaction Type
Document_Date
Transaction â Date
Document_Number
Transaction â Reference Number
Description
Transaction â Description or Memo
Debit_Amount
GL impact â Debit Amount
Credit_Amount
GL impact â Credit Amount
Debtors
The Debtors file (receivables.txt) contains all the transaction details for debtors. It gathers information from the chart of accounts, account balances, transaction records, and GL impact records. If you''re using Multi-Book Accounting, the contents are specific for the selected accounting book.
The following table describes the fields and record data sources of the Debtors report.
Field
Source field
Opening_Balance
Account Balances â Account Receivable Opening Balance of the customer.
Transaction_Type
Transaction â Transaction type
Internal_Trans_ID
Transaction â Internal ID
Customer_ID
Transaction â Customer ID
Company_Name
Transaction â Company name
Document_Date
Transaction â Date
Reference_Number
Transaction â Reference Number
Description
Transaction â Memo
Debit_Amount
GL Impact â Debit amount
Credit_Amount
GL Impact â Credit amount
Creditors
The Creditors file (payables.txt) contains all the transaction details for creditors. It gathers information from the chart of accounts, account balances, transaction records, and GL impact records. If you''re using Multi-Book Accounting, the contents are specific for the selected accounting book.
The following table describes the fields and record data sources of the Creditors report.
Field
Source field
Opening_Balance
Account Balances â Accounts Payable Opening Balance of the vendor.
Transaction_Type
Transaction â Transaction type
Internal_Trans_ID
Transaction â Internal ID
Vendor_ID
Transaction â Vendor ID
Company_Name
Transaction â Company name
Document_Date
Transaction â Date
Reference_Number
Transaction â Reference Number
Description
Transaction â Memo
Debit_Amount
GL Impact â Debit amount
Credit_Amount
GL Impact â Credit amount
Receivables Master Data
The Receivables Master Data file (receivables_master_data.txt) contains the list of customers with transactions for the period and retrieves the information from customer records.
The following table describes the fields and record data sources of the Receivables Master Data report.
Field
Source field
Customer_ID
Customer ID
Account_Description
Customer name + Default Account Receivable
Company_Name
Company name
Street_Address
Company Address
Postcode
Zip Code
Location
City
Country
Country
VAT_Registration_Number
Tax Registration Number for Germany nexus
Tax_Number
Tax Number
Payables Master Data
The Payables Master Data file (payables_master_data.txt) contains the list of vendors with transactions for the period and retrieves information from vendor records.
The following table describes the fields and record data sources of the Payables Master Data report.
Field
Source field
Vendor_ID
Vendor ID
Account_Description
Vendor name + Default Accounts Payable
Company_Name
Company name
Street_Address
Company Address
Postcode
Zip Code
Location
City
Country
Country
VAT_Registration_Number
Tax Registration Number for Germany nexus
Tax_Number
Tax Number
Tax Codes
The Tax Codes file (taxcodelist.txt) contains all the tax codes and tax rates in the system. It gathers data from the configured tax settings.
The following table describes the fields and record data sources of the Tax Codes report.
Field
Source field
Tax_Code
Name
Tax_Percentage
Tax rate
Tax_Code_Description
Description
Annual VAT Report
The Annual VAT Report file (annualvat.txt) contains all tax codes and corresponding postings (amounts) for the year. It gathers the information from the financial and transactional records. If you''re using Multi-Book Accounting, the contents are specific for the selected accounting book.
The following table describes the fields and record data sources of the Annual VAT report.
Field
Source field
Tax_Code
Tax Code â Name
Amount_Type
Tax Code â Description
Total
Total VAT on Sales â Total VAT on Purchases
Period
Total VAT on Sales â Total VAT on Purchases (For the selected period only)
Important:
To comply with the requirements of the Germany GoBD certification process, you need to use the Auto-Generated Numbering feature for the Customer ID and Vendor ID fields. Both fields must be unique. NetSuite assigns name or number for Customer ID and Vendor ID fields based on your settings at Setup > Company > Auto-Generated Numbers. For more information about how to set Auto-Generated Numbers, see
Setting Up Auto-Generated Numbering
.
Fixed Assets
The Fixed Assets file (fixedassets.txt) contains all the fixed assets for the corresponding period. The data are retrieved from the FAM Asset record. To generate this file, you must have the Fixed Assets Management SuiteApp installed in your account. For more information, see
Installing the Fixed Assets Management SuiteApp
.
The following table describes the fields and record data sources for the
Fixed Assets
file
fixedassets.txt
.
Field
Source field
Asset ID
Asset ID
Asset Name
Asset name
Asset Type
Asset type
Asset Original Cost
Original asset cost
Asset Current Cost
Current asset cost
Related Topics
Generating Localized Country Tax Reports
Viewing a Generated Country Tax Report
Making Adjustments on a Country Tax Report
Exporting a Country Tax Report
Customizing Localized Tax Returns
General Notices', 'f0afda7e86b20bbc14d99bee6fd528ea6bd3d04bb2990b10c5f988546227bf91', '{"url": "https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1539585682.html", "title": "NetSuite Applications Suite - Germany Audit Files: GoBD Data Extract", "accessed_at": "2026-01-16T21:13:45.607443+00:00", "status_code": 200, "content_type": "text/html"}', '2026-01-16T21:13:45.608310+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (20, 'website', 'https://www.mobilexpense.com/en/blog/everything-you-need-to-know-about-the-gobd-2.0', 'Everything You Need To Know About The GoBD in Germany', NULL, 'Everything You Need To Know About The GoBD in Germany
Support
Login MXP
Login Declaree
Platform
Expense management
Travel management
Mileage tracking
Per diems (Daily allowances)
Business credit cards
CO₂ tracking
Policy enforcement
Solutions
Solutions
By company size
Mid-size business
Enterprise
By region
Europe
Global
By product
Declaree
MXP
Why Mobilexpense?
Why Mobilexpense?
Our European focus
Expense automation
Expense compliance
Integrations
Marketplace
About us
Resources
Resources
Newsletter
Blogs
Guides and e-books
Webinars and events
Product updates
Customer stories
Compliance centre
Pricing
En
English
Deutsch
Nederlands
Platform
Expense management
Travel management
Per diems (Daily allowances)
Business credit cards
ESG & CO2 tracking
Policy enforcement
Solutions
By company size
Medium business
Enterprise
By region
Europe
Global
By product
Declaree
MXP
Why Mobilexpense?
Our European focus
Expense automation
Expense compliance
About us
Integrations
Resources
Blog
Guides & e-books
Webinars & events
Customer stories
Release notes
Pricing
Support
Login MXP
Login Declaree
Compliance
Everything You Need To Know About The GoBD 2.0 in Germany
by
Andreea Susanu
3
 min read
Dec. 11, 2023
Everything You Need To Know About The GoBD in Germany
5
:
24
What is the GoBD 2.0?
Meaning
GoBD
stands for
Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff
.
For non-German speakers, that’s the
principles for the proper management and storage of books, records and documents in electronic form as well as for data access
.
These principles are set forth by the German Federal Ministry of Finance on tax accounting standards for tax
compliance
and internal control. They deal with the proper electronic storing of documentation and handling of tax relevant documents. The GoBD are the foundation of a paperless, digital tax process in Germany.
History
The
German Federal Ministry of Finance
first published the GoBD on November 14th, 2014. An amended version was then published on July 11th, 2019 before being withdrawn. The latest version was published on November 28th, 2019 and came into effect on January 1st, 2020.
Application of the GoBD 2.0
The GoBD principles apply to all aspects of accounting in Germany, including
expense management
. The specifications therein are relevant for all IT systems that record or process tax-relevant data. They lay out minimum requirements for processes, systems, data security, controls and process documentation. They do not impose a format for companies to follow.
The German tax authorities assume that records which meet these requirements are traceable and can be verified, and that they cannot be tampered with. In principle, this means that documents recorded digitally in compliance with the GoBD process are legally on par with paper-based documents. Paper documents digitally recorded following this process must then be destroyed.
The GoBD in Germany also regulate auditors'' access to tax data and the scope of the guidelines. Compliance with accounting processes and logging are also addressed.
What does the GoBD 2.0 mean for German companies?
The burden of proving that a company meets the GoBD requirements lies with the company itself. German companies must provide 20+ documents to prove their GoBD compliance. These documents give the tax authority transparency on the digitalisation processes and providers used by the company. This documentation then serves to ascertain that the company can indeed rely on digital rather than paper tax documentation.
The main changes relevant to
expense report compliance
include:
Photographing receipts with a mobile device is now equivalent to regular scanning.
If all the conditions are met, the digital copy of a receipt is enough and can replace the paper original.
Location-independent mobile scanning of documents with real-time capture and digitisation is permitted (e.g. through the use of real-time
OCR with a mobile phone
) - including abroad.
Cloud systems are now explicitly included.
With these, the GoBD effectively makes life easier for travelling employees and financial controllers. Travellers now have the option to use their mobile device to scan receipts and invoices and submit them digitally for reimbursement, without being required to keep the paper originals.
How Mobilexpense supports GoBD 2.0 requirements
GoBD 2.0 documentation
To support them with their GoBD requirements, Mobilexpense offers customers operating in Germany complete GoBD 2.0 documentation, reviewed and vetted by PwC. Most documents are pre-prepared in our own format and only five must be finalised by the customer with their information.
Thanks to the latest changes to the GoBD regulation, mobile scanning, digital storing and the use of cloud systems are now all allowed. This enables our customers to follow a fully paperless expense management process.
Z1 auditor access
The GoBD 2.0 also alludes to the control of financial data. Mobilexpense enables this by providing the competent authorities with Z1 access to the audited company’s data.
The Z1 compliance level is the most challenging of a three-tiered compliance requirement regarding data access and the auditability of digital documents by the German tax authorities.
Z1 - Direct read-only access
Z2 - Indirect access
Z3 - Data carrier release / data media transfer
It is up to the tax authority which access they wish to have and they usually go back three years in the data. However, historical data going back up to ten years may be requested in cases of suspected fraud or other wrongdoing.
On demand, Mobilexpense can configure dedicated accounts to “Auditor” access. This allows the specified user to access all settled expense notes from the time period of the audit in read-only mode, as required by the GoBD.
Mobilexpense expense software and the GoBD
Our mobile apps enable travellers to scan tax-relevant documents from anywhere in the world. The apps recognise the data in real-time thanks to OCR, and store it. The entire process of expense management from capture to accounting to document entry and archiving takes place in the app. Mobilexpense also provides companies with support in the creation of procedural documentation, simplifying their GoBD compliance.
On this page:
What is GoBD 2.0?
Application of the GoBD 2.0
What does the GoBD 2.0 mean for German companies?
How Mobilexpense supports GoBD 2.0 requirements
Share this
Share on Twitter
Share on Facebook
Share on LinkedIn
Previous article
← Understanding Expense Management: A Beginner''s Overview
Next article
Securing SaaS Authentication Within Expense Applications →
You may also enjoy
these related stories
Dutch Compliance in 2025: VAT Updates and Common Mistakes
Compliance
Dutch Compliance in 2025: VAT Updates and Common Mistakes
November 18 2025
9 min read
VAT 2025: Current VAT Rates and German Examples
Compliance, VAT recovery
VAT 2025: Current VAT Rates and German Examples
October 24 2025
7 min read
Travel Expense Reports Germany: What Applies for 8hour+ Trips
Compliance
Travel Expense Reports Germany: What Applies for 8hour+ Trips
October 16 2025
9 min read
Germany Expense Allowance Table 2025: Updated Per Diem Rates
Compliance
Germany Expense Allowance Table 2025: Updated Per Diem Rates
October 16 2025
13 min read
Trusted by 3,000+ companies to automate expense management and compliance.
Receive the monthly newsletter that expense experts actually read.
Product
Expense management software
Business credit cards
Per diems
Travel management
Integrations
Mobile expense app
CO₂ tracking
Resources
Blog
Guides and e-books
Customer stories
Webinars and events
Product updates
Compliance centre
Expense glossary
Solutions
For SMEs
For Enterprises
For EU teams
For Global teams
Company
About us
About Visma
Careers
Office locations
Contact us
Support
MXP login
Declaree login
Legal and compliance
Impressum
GDPR and data security
Transparency
© 2026 Mobilexpense is now part of Visma.
Imprint
Privacy
Cookie policy
Visma Global
Visma Careers', '9de01fd755d6ba4db790d95d19a22c2f3a5b19c33bd348074bd0f981fcb57734', '{"url": "https://www.mobilexpense.com/en/blog/everything-you-need-to-know-about-the-gobd-2.0", "title": "Everything You Need To Know About The GoBD in Germany ", "accessed_at": "2026-01-16T21:13:45.769318+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:13:45.770196+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (21, 'website', 'https://www.odoo.com/blog/odoo-news-5/gobd-impact-on-german-businesses-and-their-accounting-software-selection-1529', 'GoBD Impact on German Businesses - Odoo', NULL, 'GoBD Impact on German Businesses
Skip to Content
Odoo
Menu
Sign in
Try it free
Apps
Finance
Accounting
Invoicing
Expenses
Spreadsheet (BI)
Documents
Sign
Sales
CRM
Sales
POS Shop
POS Restaurant
Subscriptions
Rental
Websites
Website Builder
eCommerce
Blog
Forum
Live Chat
eLearning
Supply Chain
Inventory
Manufacturing
PLM
Purchase
Maintenance
Quality
Human Resources
Employees
Recruitment
Time Off
Appraisals
Referrals
Fleet
Marketing
Social Marketing
Email Marketing
SMS Marketing
Events
Marketing Automation
Surveys
Services
Project
Timesheets
Field Service
Helpdesk
Planning
Appointments
Productivity
Discuss
Approvals
IoT
VoIP
Knowledge
WhatsApp
Third party apps
Odoo Studio
Odoo Cloud Platform
Industries
Retail
Book Store
Clothing Store
Furniture Store
Grocery Store
Hardware Store
Toy Store
Food & Hospitality
Bar and Pub
Restaurant
Fast Food
Guest House
Beverage Distributor
Hotel
Real Estate
Real Estate Agency
Architecture Firm
Construction
Estate Management
Gardening
Property Owner Association
Consulting
Accounting Firm
Odoo Partner
Marketing Agency
Law firm
Talent Acquisition
Audit & Certification
Manufacturing
Textile
Metal
Furnitures
Food
Brewery
Corporate Gifts
Health & Fitness
Sports Club
Eyewear Store
Fitness Center
Wellness Practitioners
Pharmacy
Hair Salon
Trades
Handyman
IT Hardware & Support
Solar Energy Systems
Shoe Maker
Cleaning Services
HVAC Services
Others
Nonprofit Organization
Environmental Agency
Billboard Rental
Photography
Bike Leasing
Software Reseller
Browse all Industries
Community
Learn
Tutorials
Documentation
Certifications
Training
Blog
Podcast
Empower Education
Education Program
Scale Up! Business Game
Visit Odoo
Get the Software
Download
Compare Editions
Releases
Collaborate
Github
Forum
Events
Translations
Become a Partner
Services for Partners
Register your Accounting Firm
Get Services
Find a Partner
Find an Accountant
Meet an advisor
Implementation Services
Customer References
Support
Upgrades
Github
Youtube
Twitter
Linkedin
Instagram
Facebook
Spotify
+32 2 290 34 90
Get a demo
Pricing
Help
GoBD Impact on German Businesses and their Accounting Software Selection
December 17, 2024
by
Marianna Ciofani (cima)
|
5
                Comments
The
GoBD
is a
regulatory framework
governing the management and storage of digital financial records, designed to ensure tax compliance and streamline accounting processes. It enables businesses to embrace digitization responsibly while meeting strict regulatory requirements. This article will help you understand the GoBD framework, its key principles, and its implications for your business. It also highlights how Odoo’s features can help your company comply with GoBD guidelines while streamlining operations and improving efficiency.
What is
GoBD
?
The GoBD (
Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff
) is a
crucial regulatory framework in Germany for managing and storing digital financial data
. Compliance with GoBD principles is not just a legal obligation but a vital part of streamlining accounting processes in the digital age.
This framework applies to all IT systems that directly or indirectly record or process tax-relevant data. Its primary goal is to
ensure traceability
,
auditability, and immutability
of financial records, enabling tax authorities to verify the correctness and completeness of these records. It also establishes that electronic documents, when recorded according to GoBD guidelines, are legally equivalent to paper documents.
GoBD
Principles
GoBD isn’t a prescriptive framework in the sense that it does not determine specific systems or formats. Instead, it outlines some key principles that all accounting systems must meet:
1. Data integrity and traceability
Data integrity and traceability are central to GoBD compliance, ensuring that tax-relevant data cannot be altered without leaving a trace. This requirement applies equally to electronic and paper records.
Software systems must log and timestamp all changes to finalized tax-relevant records, providing auditors with activity logs detailing any modifications. Essentially, digital records are treated with the same rigor as paper records.
Note that prior internal reviews or authorizations of non-finalized records remain permissible under GoBD.
2. Transparency
Transparency under the GoBD requires a complete and traceable record of all business transactions. Tax auditors must be able to track transactions and assess a company’s financial and economic situation within a reasonable timeframe.
3. Neatness
The GoBD stresses the importance of systematically organizing financial data. Accounting records must be clearly formatted and stored in an orderly manner to facilitate easy access and review by tax authorities. For example, using well-structured digital folders ensures efficient record keeping and simplifies audits.
4. Completeness
Completeness focuses on retaining all relevant records without omission. The framework outlines two retention categories:
A 10-year retention period for balance sheets, contracts, invoices, and inventory data.
A 6-year retention period for commercial letters, costings, and export documents.
Retention periods begin at the end of the preceding calendar year.
5. Timely Bookings
Under the GoBD, cash transactions must be recorded daily, regardless of the type of POS system used, while non-cash transactions should be booked within 10 days to ensure timeliness and consistency. Goods and cost accounting entries typically follow an 8-day limit, ensuring that recorded transactions remain accurate and traceable.
6. Accuracy
The principle of accuracy ensures that all records reflect the actual circumstances of the business. Records must always match their original counterparts, ensuring reliability and consistency for audits.
GoBD
Compliance
with Odoo
T
he latest version of Odoo - Odoo 18 - provides German businesses with advanced tools to meet GoBD requirements. Key features include
:
Audit Trails
Clear Record Display
Data Security
User Access Management
Auditor Presentation
Odoo logs every user action, providing full traceability and adherence to GoBD standards. Once a booking is posted, it cannot be deleted and all related changes are automatically recorded.
Our Accounting App enables the organized presentation of financial records, with features like data filtering and sorting for enhanced clarity and accessibility.
Odoo prevents data loss and ensures accurate and complete financial record management through advanced cloud-based backup services or user-managed infrastructure.
Built-in internal control systems that include tools for access control and task delegation, ensure proper data management and security.
Odoo supports ‘read-only’ user access, thus enabling both direct (Z1) and indirect (Z2) auditor access. It also enables simple data export in a machine-readable and analyzable form such as XLS, CSV, and optional XML (Z3).
Odoo is now GoBD-certified, ensuring that the software meets legal requirements for businesses in Germany. This certification highlights Odoo''s commitment to compliance and simplifies adherence to GoBD guidelines through specialized features and validated processes.
The ultimate responsibility for GoBD compliance lies with the business itself. While Odoo provides
robust digital capabilities
, it is essential to use the system in a manner that adheres to GoBD standards. As the GoBD does not require formal software certification, compliance ultimately depends on the correct application of the software within the company.
Conclusion
The GoBD framework underpins Germany''s drive toward
digital accounting
, making compliance essential for avoiding penalties and staying competitive in a digital economy.
As a
comprehensive GoBD-ready business management solution
, Odoo can help your company to navigate Germany''s regulatory landscape. From accounting and e-invoicing to sales management and manufacturing planning, Odoo streamlines operations while ensuring compliance with German regulations.
Odoo allows companies to simplify their workflows, enhance efficiency, and focus on growth.
Ready to explore how Odoo can transform your business?
Try it for free by clicking
here!
in
Odoo News
Sign in
to leave a comment
Embracing Digital Transformation: The Rise of e-Invoicing in Romania
Community
Tutorials
Documentation
Forum
Open Source
Download
Github
Runbot
Translations
Services
Odoo.sh Hosting
Support
Upgrade
Custom Developments
Education
Find an Accountant
Find a Partner
Become a Partner
About us
Our company
Brand Assets
Contact us
Jobs
Events
Podcast
Blog
Customers
Legal
•
Privacy
Security
English
الْعَرَبيّة
Català
简体中文
繁體中文 (台灣)
Čeština
Dansk
Nederlands
English
Suomi
Français
Deutsch
हिंदी
Bahasa Indonesia
Italiano
日本語
한국어 (KR)
Lietuvių kalba
Język polski
Português (BR)
română
русский язык
Slovenský jazyk
slovenščina
Español (América Latina)
Español
ภาษาไทย
Türkçe
українська
Tiếng Việt
Odoo is a suite of open source business apps that cover all your company needs: CRM, eCommerce, accounting, inventory, point of sale, project management, etc.
Odoo''s unique value proposition is to be at the same time very easy to use and fully integrated.
Website made with
Odoo Experience
on YouTube
1.
Use the live chat to ask your questions.
2.
The operator answers within a few minutes.
Watch now', '2c4e05ec41a208e4f889292eedab1fd614418cbfd033cfe78a9dff890d2e8db8', '{"url": "https://www.odoo.com/blog/odoo-news-5/gobd-impact-on-german-businesses-and-their-accounting-software-selection-1529", "title": "GoBD Impact on German Businesses", "accessed_at": "2026-01-16T21:13:46.299057+00:00", "status_code": 200, "content_type": "text/html; charset=utf-8"}', '2026-01-16T21:13:46.299832+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (22, 'website', 'https://www.hornetsecurity.com/en/services/email-archiving/', 'Email Archiving Service: Stay legally compliant with Hornetsecurity', NULL, 'Email Archiving Service: Stay legally compliant with Hornetsecurity
Skip to content
Skip to content
en
es
de
fr
ja
ca
us
Contact
24/7 Support
Partner Portal
Products
Products
HOLISTIC M365 SECURITY
365 Total Protection
All your M365 Security, Backup, GRC needs
Plan 4
Plan 3
Plan 2
Plan 1
Plan 1
SECURITY
Security Awareness Service
DMARC Manager
AI Cyber Assistant
Spam and Malware Protection
Advanced Threat Protection
Email Encryption
Email Archiving
Email Continuity Service
Email Signature and Disclaimer
Hornet.email
Hornet.email
GOVERNANCE, RISK & COMPLIANCE
365 Multi Tenant Manager for MSPs
365 Permission Manager
365 AI Recipient Validation
365 AI Recipient Validation
BACKUP
365 Total Backup
VM Backup
Physical Server Backup
Resources
Resources
BLOG
Hornetsecurity Blog
Security Lab Insights
Security Lab Insights
DIGITAL MEDIA
Webinars
Podcasts
Publications
Publications
MORE LINKS
Knowledge Base
Case Studies
Release Notes
Hornetsecurity Methodology
IT Pro Tuesday
IT Pro Tuesday
DOWNLOADS
VM Backup Downloads
Physical Server Backup Update
MSPs & Channel Partners
MSPs & Channel Partners
PARTNER
Partner Program
Partner Registration
Find a Partner
Find a Partner
DISTRIBUTORS
Find a Distributor
Find a Distributor
PARTNER PORTAL
Partner Portal Login
Company
Company
COMPANY
About us
International offices
Press Center
Awards
Analyst Relations
Case Studies
Case Studies
CAREER
Open Jobs
Benefits
Culture
Proactive Application
Employees wanted!
Employees wanted!
Events
Meet Hornetsecurity
Meet Hornetsecurity
PRIVACY
Legal notice
Privacy policy
Privacy Policy Business Contacts
Privacy Policy Services
Privacy Policy for applications
Code of Conduct
Partner Login
EMAIL ARCHIVING
email data integrity & compliance for M365 and other email servers
Send a request now!
Comments
This field is for validation purposes and should be left unchanged.
Name
(Required)
Surname
(Required)
Business email
(Required)
Phone
(Required)
Company
(Required)
Lead Relationship Type
(Required)
I am*
An IT reseller or an MSP
A distributor
Looking for a cybersecurity solution for my company
Company Size
(Required)
Company Size*
10 - 100
101 - 500
501 - 1000
1001 - 2500
2501 - 5000
5001 - 10000
10000+
Number of end-cust managed
(Required)
Number of end-cust managed*
1-249
250 - 499
500 - 1000
1000+
This field is hidden when viewing the form
Is the product DMARC or SAS?
This field is hidden when viewing the form
Product is DMARC or SAS
Inquiry details
(Required)
Free product trial
製品トライアル（無償）を希望します
Free DMARC check
DMARCチェック（無償）を希望します
Marketing
Please send me information about Hornetsecurity''s products, webinars and reports.
Policy
(Required)
I agree to the processing of my data and the establishment of contact by Hornetsecurity or a certified partner in accordance with the
data protection guidelines for business contacts
.
This field is hidden when viewing the form
Product Type
This field is hidden when viewing the form
Page Type
This field is hidden when viewing the form
Form Type
This field is hidden when viewing the form
Campaign Name
This field is hidden when viewing the form
Website Language
This field is hidden when viewing the form
url_slug
You need to enable Javascript for the anti-spam check.
Home
»
Hornetsecurity Services
»
Email Archiving
AUTOMATED AND LEGALLY COMPLIANT EMAIL ARCHIVING
Archiving business emails is a legal necessity, with several requirements to be met. Archiving must be audit-proof and legally compliant, with appropriate retention periods set in place. Archiving-related admin tasks can be time-consuming, alongside customers needing a practical way to grant access to third parties to execute audits. This is where we come in.
Easy recovery of accidentally deleted emails
If a user’s emails are accidentally deleted from the mail server, they can be restored from the archive – at any time with the simple push of a button.
Fully automated and secure
Automatic archiving of all incoming and outgoing email messages eliminates the need for administrators to perform archive-related tasks. In the cloud, all data is stored securely, unalterably and completely.
Unaltered and unalterable
In accordance with audit-proof archiving, all incoming and outgoing emails are automatically stored in their original form in Hornetsecurity’s data centers immediately upon arrival and dispatch. This ensures that no important documents are lost and archiving is complete. They cannot be edited or deleted before the set retention period has expired.
All archived emails are securely stored in
encrypted
databases in certified and secured data centers.
All features of
Hornetsecurity email archiving
at a glance
Exclusion of individual users from archiving
Marking of private mails by users
Archiving of internal emails (optional)
Unlimited storage per user included
Encryption of the transmission path between archive and mail server via TLS
Ability to regulate retention periods
REQUEST NOW
the easy management of email cloud archiving: 12 benefits
Easily find the emails you are looking for
With the daily
flood of emails
, it can be difficult to keep track of messages. Good search algorithms are needed to find messages and redisplay them without problems. In addition, it must be possible to limit and target the search using various search parameters. Hornetsecurity’s Email Archiving makes this possible thanks to its comprehensive search functions.
Full text index
The solution’s full text index makes it very easy to find the emails and attachments you are looking for. All archived messages are fully indexed so that the search time is correspondingly short.
Extensive search criteria
Apart from full text search to find archived emails, search parameters can be narrowed down by individual search criteria such as date, sender, recipient and subject. This way, searched messages can be identified more precisely and found more quickly.
Compliance, transparency and control
The legal requirements that apply to traditional letters also apply to business emails. They must be kept and retained for a certain period of time. In addition, it must be ensured that an auditor can access the emails at any time.
Audit access with 4-eyes principle
An email archive must be accessible for auditing at all times. Hornetsecurity’s Email Archiving enables audit access is provided for this purpose, giving the auditor extended read rights to the stored emails of a specific domain, which can be set up by the IT administrator. Audit access allows the auditor access to the archive for a limited time. Once the audit is complete, the administrator has access to the audit log and can see what data has been viewed.
Audit log/audit trail
The solution keeps a complete log of all access to the email archive, especially when settings such as retention periods are changed. The protocol contains, among other things, the login name and IP address of the user and it cannot be edited or deleted. The administrator can view the audit log at any time.
No access to content for administrators
In order to prevent misuse of the archived email data of individual users, administrators do not have access to archived user emails. They can only see the metadata, not the content.
Substitute regulation
Users can give another user access to their email archive, which is useful in the case of absent or retired employees.
Export of archived data possible at any time
The entire email archive can be exported easily at any time. Customers can either do this themselves, or alternatively Hornetsecurity can carry out the export for a fixed price.
Import function
Emails and their attachments can be imported from other databases into Hornetsecurity’s email archive, whether in .pst or Outlook format. This makes it easy to re-archive older emails.
Unique assignment of archives for changing mail addresses
If a user is given a new email address, archived messages can be assigned to this new address so that the user can continue to access the data.
Unicode capability
Emails that end up in Hornetsecurity’s email archive are saved in their original format, regardless of the encoding. This applies to different languages and characters as well as to encrypted messages.
What is Email archiving?
Email archiving is the process of preserving and storing email communications in a manner that is safe, organized, and accessible for future use. Organizations depend on email archiving for a variety of reasons, including compliance with laws, internal audits, potential litigation, and more. Safe and effective email archiving requires email security to protect sensitive email communications against cyberthreats and bad actors.
How to use Email archiving
There are many reasons an MSP would want to ensure the longevity and integrity of archived emails. Even when the risk of phishing has passed, your clients’ emails still need to be stored in a secure and retrievable way for legal and business reasons.
Email archiving involves capturing and preserving email content either directly from the email application itself or while it’s in motion. It’s important for archives to be stored securely and to be searchable when the need arises.
Reducing email file and attachment sizes is typically integral to an email archival system. Compression, deduplication, and low-cost cloud storage each allow email archival expenses to be kept to a minimum.
LEARN HOW YOU CAN BENEFIT
FREE DOWNLOADS
For more product details, take a look at our Fact Sheets.
Email Archiving >
365 Total Protection >
Security Awareness Service >
EDUCATIONAL CONTENT
We have some well researched content pieces for you! Watch our Webinars, read our eBooks and listen to our Podcast!
Educational content >
Webinars >
Podcasts >
More Hornetsecurity Services
Service
AI Cyber Assistant
The ultimate security power up to our solutions, enhancing them up with the latest AI and machine learning technology and automation.
Service, Service
DMARC Manager
Safeguards domains against Email impersonation, phishing, and spoofing with intuitive DMARC, DKIM, and SPF management.
Service
365 Multi-Tenant Manager
Effortless onboarding, governance, and compliance for all Microsoft 365 tenants.
Request now
Holistic M365 Security
365 Total Protection
Security
Security Awareness Service
Spam and Malware Protection
Advanced Threat Protection
Email Encryption
Email Archiving
Email Continuity Service
Email Signature and Disclaimer
Governance, Risk & Compliance
365 Permission Manager
365 AI Recipient Validation
Backup
365 Total Backup
VM Backup
Physical Server Backup
Resources
Publications
Cloud Security Blog
Webinars
Podcasts
Security Lab Insights
Release Notes
Company
About Us
International
Career
Press Center
Awards
MSPs & Channel Partners
Partner Program
Partner Registration
Partner Portal
Legal
Privacy Policy
Legal notice
Privacy for applications
Privacy Policy for Services
Privacy Policy for Business Contacts
Proofpoint’s Position on the U.S. CLOUD Act
Code of Conduct and Code of Ethics
Regional Websites
United States
Italy
Canada (french)
CONTACT US!
SALES
+44 8000 246-906
24/7
SUPPORT
+44 2030 869-833
[email protected]
© 2026 Hornetsecurity GmbH. All rights reserved
24/7 Support
Partner Portal', '29d1a650aae286608fffc4338ccbd2d9bf659d2eccbbf7d4d6e7d27d02ce46bb', '{"url": "https://www.hornetsecurity.com/en/services/email-archiving/", "title": "Email Archiving Service: Stay legally compliant with Hornetsecurity", "accessed_at": "2026-01-16T21:14:47.363664+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:14:47.364609+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (23, 'website', 'https://sourceforge.net/software/compare/Hornetsecurity-Email-Archiving-vs-Netmail/', 'Hornetsecurity Email Archiving vs. Netmail Comparison - SourceForge', NULL, 'Hornetsecurity Email Archiving  vs. Netmail Comparison
Join/Login
Business Software
Open Source Software
For Vendors
Blog
About
More
Articles
SourceForge Podcast
Support / Documentation
Subscribe to our Newsletter
Support Request
For Vendors
Add a Product
Join
Login
Business Software
Open Source Software
SourceForge Podcast
Resources
Articles
Case Studies
Blog
Menu
Help
Create
Join
Login
Hornetsecurity Email Archiving  vs. Netmail
Comparison Chart
Hornetsecurity Email Archiving
Hornetsecurity
Netmail
+
+
Learn More
Update Features
Learn More
Update Features
Add To Compare
Add To Compare
Related Products
CivicPlus Social Media Archiving
The world’s most dependable archiving software for records compliance and risk management for public entities. CivicPlus Social Media Archiving connects directly to your social networks to capture and preserve all the content your organization posts and engages with, in-context and in near-real-time. And it all lives in one easy-to-use, secure archive, so you can easily manage your online communications and help your organization stay compliant with public records laws, regulations, and recordkeeping initiatives. Social media archiving software ensures your organization’s communications are saved so you can easily respond to records requests, and remain compliant with public records laws. Capture and preserve all content you post and engage with, including deleted, edited, and hidden posts and comments. Replay records in their original context and ensure their authenticity with digital signatures and more.
14 Ratings
Visit Website
SureSync
SureSync Pro is a file replication and synchronization application that provides one-way and multi-way processing in both scheduled and real-time modes. The Communications Agent provides real-time monitors, delta copies via Remote Differential Compression, TCP communications, compression, and encryption.

SureSync Managed File Transfer (MFT) adds file locking, archiving, enhanced logging/status, blob storage support in Azure and Amazon clouds, and a next-generation intelligent transfer engine. File locking enables real-time multi-way collaborative environments with protection against users changing the same file in multiple offices at the same time.

SQL Protection simplifies backups of critical SQL databases.

SureSync comprehensive enterprise-grade feature set can help solve any file synchronization, replication, and archiving challenge.
13 Ratings
Visit Website
LogicalDOC
LogicalDOC helps organizations around the world gain complete control over document management. Focusing on business process automation and fast content retrieval, this premier document management system (DMS) allows teams to create, collaborate, and manage large volumes of documents and stores valuable company data in a centralized repository. System features include a drag-and-drop document upload, forms management, optical character recognition (OCR), duplicate detection, barcode recognition, event logging, document archiving, integrated document workflow, and so much more.

Schedule a free, no obligation, one-on-one demo today.
124 Ratings
Visit Website
Gearset
Gearset is the complete, enterprise-ready Salesforce DevOps platform, enabling teams to implement best practices across the entire DevOps lifecycle. With powerful solutions for metadata and CPQ deployments, CI/CD, testing, code scanning, sandbox seeding, backups, archiving, observability, and Org Intelligence — including the Gearset Agent — Gearset gives teams complete visibility, control, and confidence in every release.

More than 3,000 enterprises, including McKesson, IBM and Zurich, trust Gearset to deliver securely at scale. Combining advanced governance, built‑in audit trails, SOX/ISO/HIPAA support, parallel pipelines, integrated security scans, and compliance with ISO 27001, SOC 2, GDPR, CCPA/CPRA, and HIPAA, Gearset provides enterprise‑grade controls, rapid onboarding, and a user‑friendly interface — all in one platform.

Gearset delivers enterprise‑grade power without the overhead, which is why leading global organizations in finance, healthcare, and technology choose us,
228 Ratings
Visit Website
ManageEngine EventLog Analyzer
ManageEngine EventLog Analyzer is an on-premise log management solution designed for businesses of all sizes across various industries such as information technology,  health, retail, finance, education and more. The solution provides users with both agent based and agentless log collection, log parsing capabilities, a powerful log search engine and log archiving options. 

With network device auditing functionality, it enables users to monitor their end-user devices, firewalls, routers, switches and more in real time. The solution displays analyzed data in the form of graphs and intuitive reports.  

EventLog Analyzer''s incident detection mechanisms such as event log correlation, threat intelligence, MITRE ATT&CK framework implementation, advanced threat analytics, and more, helps spot security threats as soon as they occur. The real-time alert system alerts users about suspicious activities, so they can prioritize high-risk security threats.
190 Ratings
Visit Website
P3Source
Crafted by industry-savvy print experts, P3Source uses the latest SaaS technology, to automate the conventional ''Bid and Buy'' RFQ process commonly used in the Printing and Marketing Services Industry. P3Source acts as a project management and collaboration hub, where users  manage dozens of simultaneous projects, bringing together all the details, files, approvals, notes, and historical data in one easy-to-search place. It archives completed projects for future access and detailed reporting.

The P3Source web Customer and Supplier portals tie together the entire supply chain. Customers  submit requests, upload production files and approve projects. Suppliers submit quotes, accept orders, exchange files, post shipments and present invoices. This streamlined approach ensures quick, hassle-free transactions for all parties.

Celebrate the future of print management with P3Source - easy, efficient, and made with you in mind.
16 Ratings
Visit Website
Detrack
Streamline everything from proof of delivery and real-time driver tracking, through to route optimisation and customer updates. Save time, reduce operating costs, and boost productivity with Detrack. 

At a glance
- Create, manage and dispatch jobs 
- Plan and optimise routes
- Track drivers in real-time 
- Get live job updates
- Capture proof of delivery
- Create automated, branded customer comms - SMS, WhatsApp and email 
- Create digital vehicle inspections
- Get actionable data insights
- Configure workflows, fields and naming conventions
- Secure data store - up to 5 years
- Rate cards for 3PLs

A subscription includes:
- Manager dashboard and mobile app - where managers and dispatchers are in full control. Access all tools and stay up-to-date in real-time
- Driver mobile app - an easy-to-use interface for drivers to complete vehicle checks, receive and complete jobs & capture proof of delivery
- Scanner app - sort and manage parcels with ease
142 Ratings
Visit Website
NordVPN
We help companies keep their networks and Internet connections secure. Our VPN service adds an extra layer of protection to secure your communications. We do this by applying strong encryption to all incoming and outgoing traffic so that no third parties can access your confidential information. Protect your organization against security breaches. Secure remote team access. Simplify business network security. Access region-specific online content from anywhere in the world
1,720 Ratings
Visit Website
BrandMail
BrandMail®, developed by BrandQuantum, is a software solution that seamlessly integrates with Microsoft Outlook to empower every employee in the organization to automatically create consistently branded emails via a single toolbar that provides access to brand standards and the latest pre-approved content. Develop email signatures in line with your brand specifications which look consistent, no matter which device or platform they are viewed on. Your signatures are tamper-proof and centrally managed. More importantly, users see their signatures, banners and surveys when they create, reply or forward emails. BrandMail does not reroute your emails via any external servers and does not append rules to your exchange environment. It works directly within Microsoft Outlook. Leverage every email as an opportunity to brand consistently and minimize the security risks associated with the tampering of HTML signatures.
307 Ratings
Visit Website
Fax.Cloud
Designed for compliance-heavy industries, Fax.Cloud delivers encrypted, point-to-point faxing with guaranteed delivery and built-in audit trails for just 2¢ CAD per page. It meets PIPEDA, HIPAA, and SOC2 requirements, while avoiding the risks of spam filters, misdirected emails, and silent failures associated with email. Send and receive faxes anywhere using a web portal, email, desktop, or mobile device. There’s no need for phone lines, hardware, or maintenance, and all transmissions and stored documents are securely encrypted. Set up your team of Users, assign permissions and send documents in just a few clicks. Get email notifications when documents are received and keep everything organized in one place. With local and toll-free numbers and easy scalability, Fax.Cloud grows as your business grows. Upgrade to smarter, safer faxing today. Get started with Fax.Cloud.
1 Rating
Visit Website
About
Legally compliant, fully automated and audit-proof email archiving. For long-term, unchangeable and secure storage of important company information, data and files. Retrieval and recovery of archived emails. If a user’s emails are accidentally deleted from the mail server, they can be restored from the archive – at any time with the simple push of a button. Fully automated and 100% secure cloud archiving. Automatic archiving of all incoming and outgoing email messages eliminates the need for administrators to perform archive-related tasks. In the cloud, all data is stored securely, unalterably and completely. Automatic archiving: unaltered and unalterable. In accordance with audit-proof archiving, all incoming and outgoing emails are stored automatically and in their original form in Hornetsecurity’s data centers immediately upon arrival and dispatch. This ensures that no important documents are lost and archiving is complete.
About
Netmail helps you with the migration, implementation, adaptation and operation of Microsoft 365 and supports you with the necessary software so that you maintain control over your data and information. The Netmail Cloud, hosted in ISO 27001 certified data centers, offers legally compliant archiving of emails and files as well as software as a service for data and information management tasks and our managed services guarantee worry-free work with Microsoft 365 and NetGovern. E-mail archiving "as a service" in the Netmail Cloud ensures that e-mails and files are archived in compliance with the law and in an audit-proof manner. Our solutions take into account the requirements of the EU GDPR, the GOBD and the separate archiving of private emails in accordance with the Telecommunications Act.
Platforms
Supported
Windows
Mac
Linux
Cloud
On-Premises
iPhone
iPad
Android
Chromebook
Platforms
Supported
Windows
Mac
Linux
Cloud
On-Premises
iPhone
iPad
Android
Chromebook
Audience
Companies of all sizes seeking an automated and audit-proof email archiving solution
Audience
Companies that need migration and implementation of Microsoft 365
Support
Phone Support
24/7 Live Support
Online
Support
Phone Support
24/7 Live Support
Online
API
Offers API
API
Offers API
Screenshots
and Videos
View more images or videos
Screenshots
and Videos
View more images or videos
Pricing
No info
rmation
available.
Free Version
Free Trial
Pricing
No info
rmation
available.
Free Version
Free Trial
Reviews/
Ratings
Overall
5.0 / 5
ease
5.0 / 5
features
5.0 / 5
design
4.5 / 5
support
5.0 / 5
Read all reviews
Reviews/
Ratings
Overall
0.0 / 5
ease
0.0 / 5
features
0.0 / 5
design
0.0 / 5
support
0.0 / 5
This software hasn''t been reviewed yet.  Be the first to provide a review:
Review this Software
Training
Documentation
Webinars
Live Online
In Person
Training
Documentation
Webinars
Live Online
In Person
Company
Information
Hornetsecurity
Founded: 2007
Germany
www.hornetsecurity.com/us/services/email-archiving/
Company
Information
Netmail
Founded: 2001
Canada
www.netmail.com
Alternatives
Intradyn
Alternatives
Hornetsecurity Email Archiving
Hornetsecurity
MailShelf Pro
zebNet
ArcTitan
TitanHQ
OpenText MailStore Cloud Archive
OpenText
Barracuda Essentials
Barracuda Networks
Netmail
Canit-Archiver
Roaring Penguin Software
ArcTitan
TitanHQ
View All
OpenText MailStore Cloud Archive
OpenText
View All
Categories
Email Archiving
Categories
Email Management
Show More Features
Email Archiving Features
Access Control
Backup Management
Compliance Management
Data Deduplication
Data Export
eDiscovery
Encryption
Retention Management
Storage Management
Threat Protection
Show More Features
Email Management Features
Data Recovery
Email Archiving
Email Monitoring
Queue Manager
Response Management
Routing
Shared Inboxes
Signature Management
Spam Blocker
Whitelisting / Blacklisting
Integrations
Gmail
GoDaddy Email
Microsoft 365
Microsoft Outlook
TRANSEND
View All 4 Integrations
Integrations
Gmail
GoDaddy Email
Microsoft 365
Microsoft Outlook
TRANSEND
View All 1 Integration
Claim Hornetsecurity Email Archiving  and update features and information
Claim Hornetsecurity Email Archiving  and update features and information
Claim Netmail and update features and information
Claim Netmail and update features and information
Find software to compare
Suggested Software
MailShelf Pro
Compare
OpenText MailStore Cloud Archive
Compare
ArcTitan
Compare
OpenText Retain Unified Archiving
Compare
Aryson Email Archiving Software
Compare
Barracuda Cloud Archiving Service
Compare
Jatheon
Compare
OpenText MailStore Server Archive
Compare
OneVault
Compare
×
SourceForge
Open Source Software
Business Software
Add Your Software
Business Software Advertising
Company
About
Team
SourceForge Headquarters
1320 Columbia Street Suite 310
San Diego, CA
92101
+1 (858) 422-6466
Resources
Support / Documentation
Site Status
SourceForge Reviews
© 2026 Slashdot Media. All Rights Reserved.
Terms
Privacy
Privacy Choices
Advertise
×
Thanks for helping keep SourceForge clean.
X
You seem to have CSS turned off.
             Please don''t fill out this field.
You seem to have CSS turned off.
             Please don''t fill out this field.
Briefly describe the problem (required):
Upload screenshot of ad (required):
Select a file
, or drag & drop file here.
✔
✘
Screenshot instructions:
Click URL instructions:
Right-click on the ad, choose "Copy Link", then paste here →
(This may not be possible with some types of ads)
More information about our ad policies
Ad destination/click URL:', 'b27f95e2d6bd5b5be670cbb38beac722b60e4ca980bcda3adc4b150e397633de', '{"url": "https://sourceforge.net/software/compare/Hornetsecurity-Email-Archiving-vs-Netmail/", "title": "Hornetsecurity Email Archiving  vs. Netmail Comparison", "accessed_at": "2026-01-16T21:14:47.908836+00:00", "status_code": 200, "content_type": "text/html; charset=utf-8"}', '2026-01-16T21:14:47.909659+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (24, 'website', 'https://rtcsuite.com/germany-clarifies-e-invoice-archiving-rules-gobd-2025-amendment-how-businesses-must-now-store-einvoices/', 'Germany Clarifies E-Invoice Archiving Rules: GoBD 2025 Amendment', NULL, 'Germany Clarifies E-Invoice Archiving Rules: GoBD 2025 Amendment: How Businesses Must Now Store EInvoices  - RTC Suite
Cloud Platform
RTC Suite
Architecture
Benefits & Features
SAP BTP Cockpit
ERP Integration
SAP
Oracle
MS Dynamics
Other ERP
Solutions
Digital Reporting Requirements (DRR)
e-Invoicing
Invoice Reporting
ViDA (VAT in the Digital Age)
e-Waybill
Reporting
SAF-T
VAT Return
CbCR (Country by Country reports)
Intrastat Reports
Plastic Tax Reports
EC Sales List
Automation
AP Automation
e-Banking
Reconciliation
Partners
Partnership Beyond Technology
Referral Partners
Implementation Partners
Technology Alliances
Strategic Partners
Media Partners
Partnership Benefits
Become a Partner
About Us
Company
Our Story
Our Leadership
Data Privacy & Security
Quality & Service
Awards & Certificates
Global Presence
Interoperability Framework
RTC Offices
Career
Our Values
Career Opportunities
Join Us
Contact
Blog
Articles
News
Knowledge
e-Books
White Papers
Reports
Webinars
Live Webinars
On-Demand Webinars
Events
Press Release
Success Stories
FAQ
ENG
PL
TR
DE
AR
IT
FR
ES
RO
RU
Book a demo
Book a demo
Home
Blog
News
Germany Clarifies E-Invoice Archiving Rules: GoBD 2025 Amendment: How Businesses Must Now Store EInvoices
Germany Clarifies E-Invoice Archiving Rules: GoBD 2025 Amendment: How Businesses Must Now Store EInvoices
As Germany accelerates toward mandatory B2B e-Invoicing, its Ministry of Finance has […]
August 8, 2025
News
3 min read
As Germany accelerates toward mandatory B2B
e-Invoicing
, its Ministry of Finance has issued a pivotal update to the GoBD, the framework that governs how businesses manage and archive digital financial records. The
second amendment to the GoBD, issued on 14 July 2025
delivers long-awaited clarity on
how e-invoices must be stored
, especially in light of the mandatory e-Invoicing
regime introduced in
January 2025
.
Table of Contents
Toggle
The Structured Format Takes Priority
Visual Representations (PDFs) Are Optional — But Conditional
Original Format Preservation is Mandatory
10-Year Retention with Audit-Ready Access
New Guidance for Hybrid Formats (ZUGFeRD, XRechnung)
No Archiving Needed for Some Payment Service Documents
GoBD 2025: In Effect Now
The Structured Format Takes Priority
The revised GoBD confirms that businesses only need to retain the X
ML file or the structured XML part of a hybrid invoice format like ZUGFeRD
. The
PDF component is not mandatory
, unless it contains additional tax-relevant information.
This is a shift from older interpretations that often assumed visual or printable versions had to be archived alongside XML files. Germany now joins other digital VAT regimes in recognizing structured data as the authoritative source for tax purposes.
Visual Representations (PDFs) Are Optional — But Conditional
For invoices generated through a billing or ERP system,
a PDF version no longer needs to be stored
, provided that:
A human-readable copy can be regenerated at any time
There is no loss of content or discrepancy in interpretation between the structured data and the visual version
However, if a hybrid invoice (like ZUGFeRD) contains tax-relevant details
only in the PDF
, such as posting remarks, payment conditions, or notes, then that PDF must also be archived.
Original Format Preservation is Mandatory
Businesses must
store invoices in the exact format in which they were received
. For example:
If an invoice is received in XML format, that XML must be retained.
If an invoice is sent in a hybrid format, the
structured XML
part is mandatory; the
PDF
part is optional unless it includes additional relevant data.
Format conversions (e.g. XML to PDF) for internal use are permitted, but the
original version must remain intact
and accessible. The GoBD allows enhancements such as OCR (optical character recognition) during scanning, but such additions must be verified and archived.
10-Year Retention with Audit-Ready Access
E-invoices must be archived for
ten years
, and during that period, companies must:
Guarantee
integrity, authenticity, and readability
Retain the invoice in a
machine-readable format
Ensure that tax authorities can
access or request evaluations
of stored data through a read-only interface or structured export
The GoBD update strengthens the concept of
“indirect access” (mittelbarer Datenzugriff)
allowing authorities to demand that businesses analyse and provide structured invoice data themselves or via a third party.
New Guidance for Hybrid Formats (ZUGFeRD, XRechnung)
The update includes detailed clarification around
hybrid invoice formats
, such as:
ZUGFeRD invoices contain both an XML file and a visual PDF
The XML part is considered the
legally binding record
If the PDF includes any
extra tax-relevant info
, it must also be archived
Any
conversion or deletion
of the XML part (e.g. converting to TIFF) is
explicitly prohibited
No Archiving Needed for Some Payment Service Documents
The updated guidance also notes that documents
generated by payment service providers
(e.g. transaction confirmations)
do not need to be stored
, unless:
They serve as
official accounting records
They’re the
only available record
for distinguishing cash and non-cash transactions
This reduces the burden of storing ancillary documents that have no direct accounting function.
GoBD 2025: In Effect Now
These revised requirements are
effective immediately
— from
14 July 2025
. Businesses must review their current archiving practices and technical systems to ensure they are in full alignment.
As Germany continues its multi-year rollout of mandatory e-invoicing for B2B transactions, this GoBD amendment provides critical clarity for IT, finance, and compliance teams. By aligning your systems and policies now, you not only ensure legal conformity but also unlock opportunities for process automation, cost savings, and audit efficiency.
Previous Article
Singapore’s Digital Tax Evolution: Transitioning to the e-Invoicing Era
Next Article
Slovakia Launches Public Consultation on 2027 E-Invoicing and Real-Time Reporting Mandate
Leave a Reply
Cancel reply
Your email address will not be published.
Required fields are marked
*
Comment
*
Name
*
Email
*
Website
Save my name, email, and website in this browser for the next time I comment.
Δ
Archives
January 2026
December 2025
November 2025
October 2025
September 2025
August 2025
July 2025
June 2025
May 2025
April 2025
March 2025
February 2025
January 2025
December 2024
November 2024
October 2024
September 2024
August 2024
July 2024
June 2024
May 2024
April 2024
March 2024
February 2024
January 2024
December 2023
November 2023
October 2023
September 2023
Categories
Articles
e-Book
e-Invoicing
Events
Global Compliance
Knowledge
Live Webinars
News
On-Demand Webinars
Podcasts
Press Release
Reports
SAF-T
Success Stories
Webinars
You may also like
News
Tunisia 2026: e-Invoicing Extends to Services
1. What Has Changed – and What Is Proposed  1.1 Service transactions […]
November 10, 2025
2 min read
News
Latvia’s Leap into Digital Compliance: Mandatory e-Invoicing and Reporting Requirements
Exploring the New Digital Standards for B2G and B2B Transactions  In a […]
November 8, 2024
1 min read
News
Saudi Arabia’s FATOORAH Initiative: Mandatory e-Invoicing from 2025
Starting January 1, 2025, businesses in Saudi Arabia with VATable income over […]
July 14, 2024
1 min read
Company
Our Story
Awards & Certificates
Contact Us
Product
Cloud Platform
Solutions
Partners
Legal
Data Privacy & Security
Cookie Policy
Privacy Policy
Follow Us
Facebook
Twitter
LinkedIn
Youtube
Instagram
© 2026 All Rights Reserved.
Cloud Platform
RTC Suite
Architecture
Benefits & Features
SAP BTP Cockpit
ERP Integration
SAP
Oracle
MS Dynamics
Other ERP
Solutions
Digital Reporting Requirements (DRR)
e-Invoicing
Invoice Reporting
ViDA (VAT in the Digital Age)
e-Waybill
Reporting
SAF-T
VAT Return
CbCR (Country by Country reports)
Intrastat Reports
Plastic Tax Reports
EC Sales List
Automation
AP Automation
e-Banking
Reconciliation
Partners
Partnership Beyond Technology
Referral Partners
Implementation Partners
Technology Alliances
Strategic Partners
Media Partners
Partnership Benefits
Become a Partner
About Us
Company
Our Story
Our Leadership
Data Privacy & Security
Quality & Service
Awards & Certificates
Global Presence
Interoperability Framework
RTC Offices
Career
Our Values
Career Opportunities
Join Us
Contact
Blog
Articles
News
Knowledge
e-Books
White Papers
Reports
Webinars
Live Webinars
On-Demand Webinars
Events
Press Release
Success Stories
FAQ
ENG
PL
TR
DE
AR
IT
FR
ES
RO
RU
Book a demo', '4e458fe5b44d7233d8a7e576d0d39b14d9d409cca5e758b22071e7963e4ac5fe', '{"url": "https://rtcsuite.com/germany-clarifies-e-invoice-archiving-rules-gobd-2025-amendment-how-businesses-must-now-store-einvoices/", "title": "Germany Clarifies E-Invoice Archiving Rules: GoBD 2025 Amendment: How Businesses Must Now Store EInvoices  - RTC Suite", "accessed_at": "2026-01-16T21:15:10.537238+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:15:10.538012+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (25, 'website', 'https://www.dynatos.com/blog/germany-updates-gobd-rules-for-2025-e-invoicing-mandate/', 'Germany updates GoBD rules for 2025 e-invoicing mandate - Dynatos', NULL, 'Germany updates GoBD rules for 2025 e-invoicing mandate
Skip Navigation
Solutions
Themes
E-invoicing mandates
Source-to-Pay maturity
AI in finance and procurement
Process excellence
Compliance and regulations
ESG
Business Spend Management
Cloud
Peppol
XRechnung and ZUGFeRD
KSeF
View all themes
By Business Solutions
Source-to-Pay
Source-to-Pay
AP Automation
AP Automation
AR Automation
AR Automation
Intelligent Document Processing
Intelligent Document Processing
E-invoicing
E-invoicing
SAP process automation
SAP process automation
Order Confirmations
Accounts Payable
Purchase Requisitions
Delivery Notes
Sales Orders
View all business solutions
Case
Efteling
We realized a reduction of
50%
in processing time for delivery notes & invoices.
Read more
Software
Software
Routty
Routty Cloud
Routty AR
Routty AP
Routty IDP
Routty Connectors
Coupa
Microsoft
ISPnext
Tungsten Automation
Tungsten Process Director
Tungsten AP Essentials
Tungsten ReadSoft Invoices
Tungsten e-invoicing network
View all software
Industries
Pharmaceuticals
Banking
Healthcare
Supply Chain
Retail
Manufacturing
Liveblog
Croatia confirms mandatory e-invoicing from 2026
The Croatian Ministry of Finance has published…
Liveblog
France announces e-invoicing pilot phase for early 2026
The French Tax Authority has published detailed…
Resources
Discover
Portfolio
Downloads
Blog
View all resources
Attend
Events
On demand
Webinar
Thu, Jan 29
Routty Partner Winter Update 2026
Read more
Services
Customer services
Support
–
Available 24hours a day.
Implementation
–
Delivering successful projects.
View all services
Supplier onboarding
–
Supplier onboarding as a service.
Advisory
–
Qualitative digital transformation assistance.
Finance Automation
Unleashing the power of AI in Procurement and Finance
The emergence of artificial intelligence (AI) has…
Finance Automation
5 steps to a fully automated invoicing process – Delivery Notes
In the complex world of procurement and…
Company
About us
About Dynatos
Our offices
Partnerships
Become a partner
Frequently Asked Questions
View all about us
Careers
Work at Dynatos
–
Join our talented teams.
Open application
–
We are always looking for talent.
Datasheet
The company overview: solutions & services
Read more
Contact
Close
EN
DE
ES
NL
Skip Navigation
Solutions
Themes
E-invoicing mandates
Source-to-Pay maturity
AI in finance and procurement
Process excellence
Compliance and regulations
ESG
Business Spend Management
Cloud
Peppol
XRechnung and ZUGFeRD
KSeF
View all themes
By Business Solutions
Source-to-Pay
Source-to-Pay
AP Automation
AP Automation
AR Automation
AR Automation
Intelligent Document Processing
Intelligent Document Processing
E-invoicing
E-invoicing
SAP process automation
SAP process automation
Order Confirmations
Accounts Payable
Purchase Requisitions
Delivery Notes
Sales Orders
View all business solutions
Case
Efteling
We realized a reduction of
50%
in processing time for delivery notes & invoices.
Read more
Software
Software
Routty
Routty Cloud
Routty AR
Routty AP
Routty IDP
Routty Connectors
Coupa
Microsoft
ISPnext
Tungsten Automation
Tungsten Process Director
Tungsten AP Essentials
Tungsten ReadSoft Invoices
Tungsten e-invoicing network
View all software
Industries
Pharmaceuticals
Banking
Healthcare
Supply Chain
Retail
Manufacturing
Liveblog
Croatia confirms mandatory e-invoicing from 2026
The Croatian Ministry of Finance has published…
Liveblog
France announces e-invoicing pilot phase for early 2026
The French Tax Authority has published detailed…
Resources
Discover
Portfolio
Downloads
Blog
View all resources
Attend
Events
On demand
Webinar
Thu, Jan 29
Routty Partner Winter Update 2026
Read more
Services
Customer services
Support
–
Available 24hours a day.
Implementation
–
Delivering successful projects.
View all services
Supplier onboarding
–
Supplier onboarding as a service.
Advisory
–
Qualitative digital transformation assistance.
Finance Automation
Unleashing the power of AI in Procurement and Finance
The emergence of artificial intelligence (AI) has…
Finance Automation
5 steps to a fully automated invoicing process – Delivery Notes
In the complex world of procurement and…
Company
About us
About Dynatos
Our offices
Partnerships
Become a partner
Frequently Asked Questions
View all about us
Careers
Work at Dynatos
–
Join our talented teams.
Open application
–
We are always looking for talent.
Datasheet
The company overview: solutions & services
Read more
Contact
Close
Resources
/
Blog
/
Germany updates GoBD rules for 2025 e-invoicing mandate
BMF clarifies digital archiving rules for structured e-invoices
GoBD amendment sets archiving standards for XML-based invoices
The German Ministry of Finance (BMF) has updated the Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form (GoBD). This amendment comes ahead of the mandatory B2B e invoicing start date of January 1, 2025, and aims to align digital archiving requirements with structured e invoicing standards.
One of the key clarifications is that the XML component of a structured e invoice, including hybrid formats such as ZUGFeRD, is the legally relevant element for archiving. If a readable, graphical representation of the invoice can be generated from the XML, companies are no longer required to store a separate PDF version.
The amendment also reinforces that the original file format of a received invoice must be stored. Even if the invoice is converted for internal processing, the original file must remain in the archive to comply with legal retention obligations.
This update provides companies with clearer guidelines on how to handle and store e invoices in the context of GoBD, helping them prepare for the fast approaching e invoicing mandate.
The full text of the amendment is available on the BMF website:
GoBD 2nd amendment (PDF)
Key takeaways
From January 1, 2025, B2B e invoicing becomes mandatory in Germany.
XML is the legally relevant element for archiving structured e invoices.
No separate PDF storage is needed if a visual version can be generated from XML.
The original file format must always be archived, even after conversion.
Companies should review their archiving processes to ensure GoBD compliance.
Additional resources
The official BMF document is relevant for accounting departments, compliance officers, ERP managers, and IT teams responsible for invoice processing and archiving. You can read the full text here:
Download the GoBD amendment (PDF)
Share with your peers
Related documents
Liveblog
January 10, 2025
Slovakia’s push for e-invoicing and VAT modernization
The Slovak government has released preliminary information on an essential…
Read more
Liveblog
April 25, 2025
Sweden backs the VIDA package and e-invoicing
The Swedish Tax Agency is fully backing the EU’s ViDA…
Read more
People behind the Process
December 18, 2025
Jesper Tinggaard Rasmussen: “Look at the opportunities, not the limitations”
When you talk to Jesper Tinggaard Rasmussen, you immediately sense…
Read more
Want to know more about Dynatos?
Let’s talk
Our software
Tungsten Automation
ISPnext
Coupa
Microsoft
Routty
Our resources
Portfolio
Downloads
Events
On demand
Blog
Our company
About us
Become a partner
Support
FAQ
See all
What can we do for you?
Contact us
©2026 – Dynatos. All Rights Reserved.
Privacy policy
Cookie policy
Menu', 'f1f0f7464562ebdf6762eb15f23e5183a3ea316c879cec56d33f76e924047fdb', '{"url": "https://www.dynatos.com/blog/germany-updates-gobd-rules-for-2025-e-invoicing-mandate/", "title": "Germany updates GoBD rules for 2025 e-invoicing mandate", "accessed_at": "2026-01-16T21:15:12.174620+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:15:12.175340+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (26, 'website', 'https://www.taxathand.com/article/40479/Germany/2025/MOF-publishes-further-administrative-guidance-on-mandatory-domestic-B2B-e-invoicing', 'E-invoicing - Deloitte | tax@hand', NULL, 'Access Denied
Sorry, access is denied!
Weâve identified an issue and prevented your access.
The details: Deloitte WAF Solution
Client IP:
2a02:908:c20c:4f00:dabb:c1ff:fe96:f489
Reference ID:
18.e6656b8.1768598112.65a11935
Need help? No problem. Get in touch with Global Service Desk and provide the
Reference ID
, and weâll see how we can help.
Global ServiceNow Portal:
Report an Issue
Assignment Group:
DTTL-Cybersecurity-WebApplicationFirewall
Global WAF ServiceNow form:
Web Application Firewall (WAF)', '1079091e8bc8faf3b20242b45c70272dbe6f31d8425634a9db3f1709783ff1ee', '{"url": "https://www.taxathand.com/article/40479/Germany/2025/MOF-publishes-further-administrative-guidance-on-mandatory-domestic-B2B-e-invoicing", "title": "Access Denied", "accessed_at": "2026-01-16T21:15:12.560311+00:00", "status_code": 200, "content_type": "text/html"}', '2026-01-16T21:15:12.563014+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (27, 'website', 'https://eclear.com/article/mandatory-e-invoicing-from-2025-vat-pitfalls-and-practical-recommendations/', 'Mandatory E-Invoicing in Germany 2025: VAT Risks & Tips - eClear', NULL, 'Mandatory E-Invoicing in Germany 2025: VAT Risks & Tips
Products
For marketplaces and platforms
ClearVAT
Cross-border E-Commerce VAT-free
ClearCustoms
Border-free commerce
Corporate Tax
CustomsAI
Customs Tariff Classification
VATRules
Database for VAT Rates and Rules
FileVAT
VAT declaration for EU-27, CH, NO, UK
CheckVAT ID
Audit-proof VAT ID Check
Solutions
Integrations
CheckVAT ID Online-Tool
SAP
Newsroom
News
Latest Information
Events
Newsletter
eClear in the media
Special topics
Cross-border E-Commerce
Customs - Infos & News
VAT - Infos & News
Knowledge:
ViDA for marketplaces and platforms
EU Directive on Administrative Cooperation (DAC)
Import-One-Stop-Shop (IOSS)
One-Stop-Shop (OSS)
VAT Rates in Europe
EU-Asia Trade Simplified: Your Customs and Compliance Guide
Tech:
Tax Technology
E-Commerce Tools
About us
Mission Europe
Management Board
Supervisory Board
Partners
Locations
Career
Contact
EN
DE
Close
Products
Products
For marketplaces and platforms
ClearVAT
Cross-border E-Commerce VAT-free
ClearCustoms
Border-free commerce
Corporate Tax
CustomsAI
Customs Tariff Classification
VATRules
Database for VAT Rates and Rules
FileVAT
VAT declaration for EU-27, CH, NO, UK
CheckVAT ID
Audit-proof VAT ID Check
Solutions
Solutions
Integrations
CheckVAT ID Online-Tool
SAP
Newsroom
Newsroom
News
Latest Information
Events
Newsletter
eClear in the media
Special topics
Cross-border E-Commerce
Customs - Infos & News
VAT - Infos & News
Knowledge:
ViDA for marketplaces and platforms
EU Directive on Administrative Cooperation (DAC)
Import-One-Stop-Shop (IOSS)
One-Stop-Shop (OSS)
VAT Rates in Europe
EU-Asia Trade Simplified: Your Customs and Compliance Guide
Tech:
Tax Technology
E-Commerce Tools
About us
About us
Mission Europe
Management Board
Supervisory Board
Partners
Locations
Career
[wpdreams_ajaxsearchlite]
Contact
EN
DE
Home
·
Newsroom
·
E-Invoicing
·
Mandatory E-Invoicing from 2025 – VAT Pitfalls and Practical Recommendations
E-Invoicing
,
Newsroom
|  12. January 2026
Mandatory E-Invoicing from 2025 – VAT Pitfalls and Practical Recommendations
Mandatory e-invoicing from 2025 is far more than a technical requirement. It directly affects key VAT principles, particularly the right to deduct input VAT. Companies that address the transition early from a VAT perspective can minimize risks while benefiting from more efficient processes.
by
eClear
1. Background: What Will Change from 2025?
With the German Growth Opportunities Act (Wachstumschancengesetz), the mandatory use of e-invoices in domestic B2B transactions will be introduced in stages. From 1 January 2025, businesses must be able to receive e-invoices; from 2027 or 2028 (depending on annual turnover), they will also be required to issue e-invoices.
From a VAT perspective, it is important to note that the legal definition of an invoice remains unchanged and continues to be based on Section 14 of the German VAT Act (UStG). However, an invoice will only qualify as an e-invoice if it is issued in a structured electronic format (e.g. XRechnung or ZUGFeRD version 2.0.1 or higher) that allows for automated processing.
2. Distinction: E-Invoice vs. Other Electronic Invoices
A common misconception in practice is to treat PDF invoices as e-invoices. For VAT purposes, the distinction is clear:
E-invoice: structured electronic format
Other electronic invoice: PDF, scanned document, email attachment
From 2025 onwards, PDF invoices will generally no longer be sufficient for domestic B2B transactions, unless a transitional rule applies. Companies must therefore ensure that they are able to clearly distinguish between different invoice types from both a technical and organizational perspective.
3. Input VAT Deduction: Where Are the Risks?
One of the most significant VAT risk areas concerns the right to deduct input VAT. As before, this right requires a proper invoice. If an e-invoice does not comply with formal requirements or uses an invalid format, the input VAT deduction may be denied during a tax audit.
Particularly critical issues include:
missing or incorrect mandatory invoice details (Section 14(4) UStG)
non-compliant data formats
media discontinuities between e-invoicing and accounting systems
missing linkage between the invoice and the underlying supply or service
Increased automation also means that errors may occur systematically and on a large scale, amplifying potential risks.
4. Impact on Internal Processes
The introduction of e-invoicing is not merely an IT project. VAT-relevant processes affected include:
invoice receipt and verification
approval and posting workflows
archiving in compliance with GoBD requirements
interfaces between ERP systems, accounting, and tax modules
Companies should review whether VAT checks are carried out before or only after posting, as this can be decisive in minimizing risks.
5. VAT-Focused Recommendations for Action
To reduce VAT risks, companies should take early and structured action:
Analyze invoice flows (incoming and outgoing invoices)
Verify invoice formats for VAT compliance
Adapt input VAT controls to automated processes
Train relevant departments (not IT alone)
Ensure close coordination between tax, accounting, and IT teams
6. Conclusion
Mandatory e-invoicing from 2025 is far more than a technical requirement. It directly affects key VAT principles, particularly the right to deduct input VAT. Companies that address the transition early from a VAT perspective can minimize risks while benefiting from more efficient processes.
Author
eClear
More articles by eClear
Write email
Share on LinkedIn
Share on Twitter
Share on Facebook
Links
EU-wide VAT Gap Report 2023 – VAT Revenue Losses and Solutions
Mandatory VAT Reporting in Digital Commerce – New Transparency Requirements in Focus
New EU VAT Rules for Imports Starting 2028 – What Businesses and Consumers Need to Know
More on the subject:
E-Invoicing
E-Invoicing
| 22. September 2023
E-Invoicing in the EU: The Quick Business Guide
In the dynamic world of e-commerce, the ability to adapt and evolve is not just a competitive advantage but a…
Customs
,
E-Commerce
,
Market insights
,
Newsroom
,
VAT
| 19. September 2023
Malta Clarifies DAC 7 for Platforms
This week''s Commerce Updates brings you critical insights: From Malta''s latest clarification on DAC 7 guidelines affecting platform operators to…
Customs
,
E-Commerce
,
Market insights
,
Newsroom
,
Payment
,
VAT
| 1. August 2023
Spain’s EU Presidency: A New Era for Taxation
Welcome to this edition of the Commerce Updates, where we bring you the latest developments shaping the world of trade…
You might also be interested in:
Newsroom
,
VAT
| 19. December 2025
EU-wide VAT Gap Report 2023 – VAT Revenue Losses and Solutions
The EU-wide VAT Gap Report 2023 published by the European Commission shows that the gap between theoretically due VAT and…
E-Commerce
,
Newsroom
,
VAT
| 4. December 2025
Mandatory VAT Reporting in Digital Commerce – New Transparency Requirements in Focus
The digitalization of commerce is prompting tax authorities across Europe to introduce new instruments to combat VAT fraud and increase…
Newsroom
,
VAT
| 28. November 2025
New EU VAT Rules for Imports Starting 2028 – What Businesses and Consumers Need to Know
From July 1, 2028, sellers and online marketplaces – including those outside the EU – will be required to collect…
Products
ClearVAT
ClearCustoms
CustomsAI
VATRules
FileVAT
CheckVAT ID
Company
About us
Management Board
Supervisory Board
Partners
Locations
Career
Newsroom
Current information
Newsletter
Glossary
Contact
eClear AG
Französische Straße 56-60
10117 Berlin, Germany
info@eclear.com
WKN: A2AA3A
ISIN: DE000A2AA3A5
Contact
Customer support
Social
©2026 eClear Aktiengesellschaft
Privacy policy
Imprint
Contact', '75f317646d65f2e1381afb75c8c98fc59edb9ced1a5e656a3237e463f98fb04e', '{"url": "https://eclear.com/article/mandatory-e-invoicing-from-2025-vat-pitfalls-and-practical-recommendations/", "title": "Mandatory E-Invoicing in Germany 2025: VAT Risks & Tips", "accessed_at": "2026-01-16T21:15:13.633897+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:15:13.634689+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (28, 'website', 'https://www.avalara.com/blog/en/europe/2024/03/germany-mandatory-e-invoicing-2025.html', 'Germany has implemented mandatory e-invoicing January 2025', NULL, 'Germany has implemented mandatory e-invoicing January 2025
Skip to main content
Agentic AI
Solutions
Browse by tax type
VAT
Streamline VAT determination, e-invoicing, and reporting
Sales tax
Retail, ecommerce, manufacturing, software
Import One-Stop Shop (IOSS)
Simplify VAT registration requirements
Selling Internationally
Customs duties and import taxes
Industries
Retail
For online, ecommerce and bricks-and-mortar shops
Software
For apps, downloadable content, SaaS and streaming services
Manufacturing
For manufacturers with international supply chains
Browse by Business Type
Accounting professionals
Partnerships, automated solutions, tax research, education, and more
Business process outsourcers
Better serve the needs of clients
Marketplace sellers
Online retailers and ecommerce sellers
Shared service centres
Insource VAT compliance for your SSC
Business Size
Enterprise
An omnichannel, international sales solution for tax, finance, and operations teams
Products
Overview
Our platform
Product categories
Calculations
Calculate rates with AvaTax
Returns & Reporting
Prepare, file and remit
VAT Registrations
Manage registrations, simply and securely
VAT Solutions
Streamline VAT determination, e-invoicing, and reporting
Fiscal
Fiscal representation
Content, Data, and Insights
Research, classify, update
Exemption Certificate Management
Collect, store and manage documents
Featured Products
Avalara E-Invoicing and Live Reporting
Compliant in over 60 countries
Making Tax Digital (MTD)
Comply with MTD Phase 2
See all products
Resources
Learn and connect
Blog
Tax insights and updates for Europe
Webinars
Free advice from indirect tax experts
Events
Join us virtually or in person at Avalara events and conferences hosted by industry leaders
Whitepapers
Expert guidance and insights
Featured Resources
Reverse Charge VAT
VAT and customs guidance
Digitalisation of tax reporting
Realtime VAT compliance (including MTD)
Selling into the USA
Sales tax for non-US sellers
Know your nexus
Sales tax laws by U.S. state
Free tools
EU Rates
At-a-glance rates for EU member-states
Global Rates
At-a-glance rates across countries
U.S. Sales Tax Risk Assessment
Check U.S. nexus and tax responsibilities
EU VAT Rules
EU VAT Registration
EU VAT Returns
Distance Selling
EU VAT digital service MOSS
Resource center
Partners
Existing Partners
Partner Portal
Log in to submit referrals, view financial statements, and marketing resources
Submit an opportunity
Earn incentives when you submit a qualified opportunity
Partner Programs
Become a partner
Accountant, consulting, and technology partners
Become a Certified Implementer
Support, online training, and continuing education
Find a partner
Avalara Certified Implementers
Recommended Avalara implementation partners
Developers
Preferred Avalara integration developers
Accountants
State and local tax experts across the U.S.
Integrations
Connect to ERPs, ecommerce platforms, and other business systems
About
About
About Avalara
Customer stories
Locations
Jobs
Get started
Get started
Sales
phone_number
Sign in
Blog
Blog
Location
North America
India
Europe
Tax type
Sales tax
Use tax
VAT
GST
Duties and tariffs
Property tax
Excise tax
Occupancy tax
Communications tax
Need
AI
Tax calculation
Tax returns
E-invoicing
Exemption certificates
Business licenses
Registrations
1099 and W-9
Cross-border
Tax changes
Sales tax holidays
IOSS and OSS
Sales tax nexus
Digital goods and services
Shipping
Online selling
Product taxability
Industry
Manufacturing
Retail
Software
Hospitality
Short-term rentals
Accounting
Communications
Government
Supply chain and logistics
Energy
Tobacco
Beverage alcohol
Business and professional services
Restaurants
Marketplace facilitators
Related
Resource center
Webinars
Share:
Share to Facebook
Share to Twitter
Share to LinkedIn
Copy URL to clipboard
German e-invoicing mandate updates
Kamila Ferhat
May 12, 2025
Last updated on May 12, 2025.
Get the latest updates on e-invoicing mandates and live reporting requirements in Germany. Businesses operating in Germany should stay informed about these developments and take necessary steps to help ensure they can operate compliantly as the e-invoicing landscape evolves.
Germany e-invoicing mandate timeline
May 2025: Germany updates national e-invoicing format
Electronic Invoice Forum Germany (FeRD) announced on May 7, 2025, an update to ZUGFeRD to coincide with France’s Factur-X1.07 update. ZUGFeRD 2.3 includes updates to Code Lists to align with requirements set out in EN16931, plus editorial and schematron corrections.
January 2025: B2B e-invoicing begins in Germany
The first phase of Germany’s business-to-business (B2B) e-invoicing mandate comes into effect, requiring German businesses to be able to receive e-invoices for B2B transactions. This requirement applies to all businesses, regardless of size or annual turnover. Acceptable e-invoicing formats must be compatible with standards outlined in European Norm (EN) 16931, such as XRechnung and ZUGFeRD. There are a small number of exemptions, including invoices under €250 and tickets for passenger transport. As part of Germany’s phased approach to implementing e-invoicing, the requirement to issue e-invoices for businesses of all sizes and turnover will be in place by January, 2028.
October 2024: Germany offers further e-invoicing guidance for businesses
The Federal Ministry of Finance (BMF) publishes the final version of guidance
for implementing Germany’s B2B e-invoicing mandate, being introduced from January 1, 2025. The guidance includes details on e-invoicing requirements and accepted formats, such as XRechnung and ZUGFeRD.
August 2024: Germany details ‘soft approach’ to e-invoicing
Germany will take a soft approach to implementing e-invoicing
, with transition periods and flexibility for smaller businesses in particular. German authorities clarify that while all businesses must be capable of receiving e-invoices by January 2025, issuing them remains optional until 2027. Germany also clarifies that from January 2025, an e-invoice will be defined as “an invoice that is issued, transmitted and received in a structured electronic format and enables electronic processing” — such as XRechnung. Standard PDFs created, transmitted, and received electronically will not be considered e-invoices.
March 2024: Germany announces mandatory e-invoicing from January 2025
The Growth Opportunities Act introduces various tax measures, including a mandate for B2B e-invoicing.
Germany outlines plans to implement e-invoicing in phases
, starting with a mandate to receive structured e-invoices from January, 2025, followed by a broader requirement to issue structured e-invoices from January 2028.
November 2022: Germany seeks EU e-invoicing mandate approval
The German government formally
asks the European Commission for permission to mandate e-invoicing
, beginning with B2B transactions. The government believes that e-invoicing will “...significantly reduce the susceptibility to fraud of our VAT system and modernise and at the same time reduce the bureaucracy of the interface between the administration and the businesses.”
Ready for e-invoicing?
Avalara E-Invoicing and Live Reporting
can help you comply with global mandates and reporting requirements as they evolve.
Share:
Share to Facebook
Share to Twitter
Share to LinkedIn
Copy URL to clipboard
Cross-border
E-invoicing
Germany
Tax and compliance
Sales tax rates, rules, and regulations change frequently. Although we hope you''ll find this information helpful, this blog is for informational purposes only and does not provide legal or tax advice.
Kamila Ferhat
Avalara Author
Recent posts
Dec 19, 2025
Preparing for Making Tax Digital in 2026: What U.K. businesses should know
Dec 16, 2025
Unlocking global e-invoicing: How enterprises can scale strategically
Nov 25, 2025
The end of the €150 customs duty exemption for low-value imports into the EU: What businesses need to know
Avalara Tax Changes 2026 is here
The 10th edition of our annual report engagingly breaks down key policies related to sales tax, tariffs, and VAT.
Read the report
Stay up to date
Sign up for our free newsletter and stay up to date with the latest tax news.
About Avalara
About Avalara
Careers
Customer insights
Partner program
Support
Products & Services
Avalara VAT Reporting
VAT Registration & Returns
AvaTax Calculation software
MTD Cloud
Avalara E-Invoicing and Live Reporting
Resources
VATLive blog
Webinars
Whitepapers
Get EU VAT number
Help with VAT returns
Contact Us
+44 (0) 1273 022400
Monday – Friday
8:00am – 6:00pm
Monday - Friday
8:00 a.m.-6:00 p.m. GMT
Europe (English)
Europe (English)
Australia (English)
Brazil (English)
Brasil (Português)
France (Français)
Germany (Deutsch)
India (English)
New Zealand (English)
Singapore (English)
United States (English)
Terms
Cookies
Privacy
Anti-Slavery Disclosure
© Avalara, Inc. {date}', '520159e85594d37b79b4881df85eb37783b154204c70f84bdc0a56587e6cd9f1', '{"url": "https://www.avalara.com/blog/en/europe/2024/03/germany-mandatory-e-invoicing-2025.html", "title": "Germany has implemented mandatory e-invoicing January 2025", "accessed_at": "2026-01-16T21:15:13.846790+00:00", "status_code": 200, "content_type": "text/html;charset=utf-8"}', '2026-01-16T21:15:13.847518+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (29, 'website', 'https://www.flick.network/en-de/gobd-electronic-archiving-requirements', 'Electronic Archiving Requirements under GoBD in Germany', NULL, 'GoBD Electronic Archiving Requirements | Germany Compliance Guide
Products
E-Invoicing Softwares & Other Offerings
E-Invoicing in UAE
Get Started with FTA E-Billing Regulations
E-Invoicing in Saudi Arabia
Get complied for ZATCA E-Fatoora Mandates
Treasury Management Suite
Be confident with Organizational liquidity
VeriFactu Solution in Spain
Get covered for Fiskalisation in Spain
E-Invoicing Solution in Belgium
Get complied for Belgium
E-Invoicing Solution in Poland
Get complied for Poland
E-Invoicing in Malaysia
Integrate with IRBM/LHDN MyInvois Portal
E-Invoicing in Singapore
Get complied for Singapore
Global e-Invoicing
Get complied for e-Invoicing Mandates globally
Resources
Learning Resources
Our Blog
Read all updates happening around the globe
Announcements
Get notified With all new announcements that just released
Case studies
Learn how our customers solved their business struggles
Demo Videos
Watch how our system works in real-world scenarios
Our Webinars
Request On-Demand Webinars from our Experts
About Company
Learn about Flick and the vision we have for your future
featured Products
E-Invoicing in Saudi Arabia
Get complied for ZATCA E-Fatoora Mandates
E-Invoicing in UAE
Get Started with FTA E-Billing Regulations
E-Invoicing in Malaysia
Integrate with IRBM/LHDN MyInvois Portal
E-Invoicing in Singapore
Get Started with InvoiceNOW requirements
VeriFactu Solution in Spain
Get covered for Fiskalisation in Spain
E-Invoicing Solution in Belgium
Get covered for Peppol in Belgium
E-Invoicing Solution in Poland
Get covered for Peppol in Poland
Integrations
Customers
Partners
Support Desk
Contact Us
Our Products:
Recently Published
Current Status of B2B E-Invoicing in Germany (2025 Update)
Mandatory E-Invoice Reception in Germany – January 2025
Germany’s Phased E-Invoicing Timeline (2025–2028)
Allowed Invoice Formats in France & Germany (2025–2028)
BMF Clarifications on Germany’s E-Invoicing Mandate (June 2025 Draft)
Electronic Archiving Requirements under GoBD in Germany
E-Invoicing in Germany – Requirements, Deadlines, and Compliance Guide
Corporate Tax in Germany – Rates, Compliance, and Filing Guide (2025)
Personal Income Tax in Germany 2025: Rates, Deductions, and Filing Deadlines
VAT in Germany 2025: Rules, Registration, and Compliance Guide
Home
/
•
Germany e-Invoicing
/
•
Electronic Archiving Requirements Under Gobd In Germany
Electronic Archiving Requirements under GoBD in Germany
F
Flick team
•
Last updated at
December 10, 2025
Book a Demo
Learn more about this by booking a demo call with us. Our team will guide you through the process and answer any questions you may have.
Book Now
Electronic archiving requirements (GoBD)
With the digitalization of businesses today, the shift from paper records to electronic systems has significantly enhanced the operations'' efficiency. It has also brought new issues in the sense of data security, authenticity, and access. Electronic data is inherently more vulnerable to tampering with data, unauthorized use, and theft compared to physical documents and therefore poses a serious compliance and security risk to organizations.
To address such challenges, regulatory bodies and governments of the world have developed effective frameworks of electronic data management. In Germany, the "Principles for the Proper Management and Storage of Books, Records, and Documents in Electronic Form and for Data Access" (GoBD) is a standardized approach to retaining the integrity, authenticity, and accessibility of the records in electronic form.
This guide outlines the essential prerequisites of the GoBD according to electronic archiving, its relevance, as well as how companies can implement compliant archiving systems for ensuring operational and legal security.
Understanding GoBD Requirements
The GoBD aims to standardize electronic recordkeeping through three core objectives:
Integrity – All records so that they are complete and accurate.
Authenticity – Making sure that documents are original and tamper-free.
Accessibility – Offering secure but reliable access to the records at the required time.
To meet these aims, requirements of GoBD harmonize with tax legislation, data security laws, and accounting principles. The guiding rules are:
Completeness and Consistency: Each business transaction has to be completely recorded to provide dependable records.
Auditability and Verifiability: The records should enable the auditors and tax administrations to reconstruct and verify business procedures in full.
Data Access and Documentation: Companies have to be in a position to produce requested documents on time in an auditor-readable format.
GoBD requirements apply to different document types, such as accounting records, invoices, contracts, and corresponding business communication.
Importance of Archiving for GoBD Compliance
Archiving is not just a compliance requirement—it''s an essential business and risk management process:
Data Integrity and Authenticity Maintenance: Documents stored must be tamper-evident with the same original metadata and content.
Simplification of Inspections and Audits: Organized archives enable auditing by simple access to documents at the location.
Legal Compliance: Authentic evidence, well-maintained documents neutralize the risk of fines or lawsuits.
Business Risk Reduction: Effective archiving protects organizations from illegal use, data loss, and business disruption.-
Increased Business Efficiency: Systemized records strengthen the exchange of information, and that leads to more efficient decision-making along with business reactivity.-
GoBD Archiving Requirements
In order to stay compliant, companies need to ensure that their computer files meet the following GoBD requirements:
Durability and Retention: Data should be stored in a stable form for the legislatively prescribed retention period without loss of data or metadata.
Unalterability and Auditability: Files submitted should be tamper-proof, and changes should be traced by an audit trail.
Accessibility and Retrieval: Companies should be able to retrieve documents along with their related metadata at the time of request.
Format and Structure: Archives must maintain original document readability and structure regardless of formats or software systems in the future that will experience changes.
Security and Confidentiality: Archives should be shielded from unauthorized access and loss of information by access control, encryption, and authentication.
Regular Auditing and Review: Procedures for archiving should be regularly reviewed to find gaps in compliance and maintain constant compliance with GoBD requirements.
Implementing GoBD-Compliant Archiving Systems
To become completely compliant, corporations should follow a methodical process:
Assess Current Practices: Identify where existing recordkeeping processes are lacking and where they are non-compliant.)
Define Archiving Policies: Create clear policies for document classification, retention, access, and auditing.)
Choose Archiving Solutions: Choose those systems that can provide for unalterability, secure storage, and scalability.
Implement Technical Controls: Impose encryption, access control, and metadata tagging in order to maintain compliance.
Train Employees: Educate personnel on archiving rules and the relevance of compliance with GoBD obligations.
Document Policies and Systems: Keep records in full for policies, settings, and audit trails to prove compliance./
Monitor and Audit Regularly: Conduct internal audits and quality checks to verify ongoing compliance with regulations.-
Conclusion
Compliance with GoBD, is not merely a regulatory requirement, but also a commercial imperative for the modern digital economy. Organizations are able to protect their data, facilitate audit ease, reduce business risk, and enhance operating efficiency through the use of secure electronic archiving systems.
Pre-emptive compliance with GoBD bookkeeping standards improves business resiliency, protects against fines from regulatory bodies, and improves stakeholder trust. For German-based companies as well as those trading with the German market, electronic compliant book-keeping is required for sustainable growth and achievement.
Quick Navigation
Book a Demo
Learn more by booking a demo with our team. We''ll guide you step by step.
Book Now
Flick Network is a leading provider of innovative financial technology solutions, specializing in global e-invoicing compliances, PEPPOL & DBNAlliance integrations, AP/AR automations, Treasury & Cash Management.
Solutions
E-Invoicing in Malaysia
E-Invoicing in Saudi Arabia
E-Invoicing in UAE
E-Invoicing in Singapore
Treasury Management
General
Home Page
About Company
Contact us
Blog Updates
Sitemap
Resources
Developer Portal
System Status
Documentations
Raise a Ticket
Integration Videos
Flick Network ©️
2026
Privacy Policy
Terms and Conditions', '6557a70566d754e81ea537b000b2662f8f6c45f25bc00474a6a2baeefaa41708', '{"url": "https://www.flick.network/en-de/gobd-electronic-archiving-requirements", "title": "GoBD Electronic Archiving Requirements | Germany Compliance Guide", "accessed_at": "2026-01-16T21:18:05.127175+00:00", "status_code": 200, "content_type": "text/html; charset=utf-8"}', '2026-01-16T21:17:51.475191+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (30, 'website', 'https://teamdrive.com/en/knowledge/german-gobd/', 'GoBD - Principles for electronic accounting - TeamDrive', NULL, 'GoBD - Principles for electronic accounting
chatbot
Chatbot is loading...
Skip to content
Search
Blog
FAQ
WEB APP
LOGIN
LOGIN
+49
40 607709 300
Deutsch
English
+49
40 607709 300
Search for:
Full Site results
FAQ only results
Solution
TeamDrive
Professional
The secure cloud solution for
business customers
Show solution
TeamDrive
KMU/­Enterprise
Extensions for
large companies, authorities, ...
Show solution
Data exchange
Data exchange via the Internet (data room)
Send large files via link
Sending attachments via Outlook Add-in
Automatic data synchronization
Receive large files (Inbox)
Security
End-to-End Encryption
Secure sending of email attachments
Ransomware protection
Zero Knowledge Cloud
Secure mobile working (SecureOffice)
Documentation
Traceability through activity log
Monitoring retention periods
Immutable archiving
Location-independent work
Data access without VPN
Across companies
Multi-platform support
Data backup
Backup and data recovery
File versioning
Certified security
GDPR compliance
EuroPrise certificate
Free choice of server
Choice of cloud, hybrid or on-premise
Hosted cloud services in Germany (SaaS)
Azure Cloud / OneDrive can be selected as storage locations
Integrations
External authentication (Azure AD)
Integrate directory services (AD, Shibboleth, OAuth 2.0)
Integrations into third-party systems (API)
Workflow automation
Form integration (authorities/­healthcare/­orders)
Scan with Mobile Sandbox (Photos)
Connection of downstream systems
Individualization
Customization of external TeamDrive download pages
Customization of the inbox function
Individual email design
Security & Compliance
Monitoring retention periods
Unchangeable archives and logs (GoBD)
Immutable data storage (ransomware protection)
Users
TeamDrive can be used by
companies of all sizes
.
Companies
Self-employed people
Freelancers
One-man businesses
Small and medium-sized enterprises (SMEs)
Healthcare
Doctor''s offices
Pharmacies
Laboratories
Clinics
Hospitals
Research institutes
Finance and Legal Affairs
Tax consultants
Lawyers
Notaries
Auditors
Authorities / public administration
Universities
School administrations
Municipalities
District offices
State parliaments
Government organizations
Non-profit organizations
Societies
Associations
Daycare centers
Churches and parish offices
Pupils and students
Industry
Automobile manufacturing
Mechanical engineering
Transport/Vehicle driver
Journalism
Publishers
Investigative journalism
Whistleblower
Other
Works councils
Recruiter
Compliance
EuroPriSe
Certification history
TeamDrive Cloud certificates
The German GoBD
Health Care – HIPAA
The German Hospital Federation
TISAX
ITAR
CCPA
Knowledge
TeamDrive FAQ
Video tutorials
TeamDrive in comparison
Cloud Computing
Backup
ePrivacy, GDPR
E-Invoicing
The German GOBD
Encyrption
Ransomware
Security by Design
Shop
Downloads
TeamDrive App
TeamDrive Outlook Add-In
TeamDrive Server App
TeamDrive Personal Server
TeamDrive manuals
Remote Support
Solution
TeamDrive
Professional
Data exchange
Data exchange via the Internet (data room)
Send large files via link
Sending attachments via Outlook Add-in
Automatic data synchronization
Receive large files (Inbox)
Security
End-to-End Encryption
Secure sending of email attachments
Ransomware protection
Zero Knowledge Cloud
Secure mobile working (SecureOffice)
Documentation
Traceability through activity log
Monitoring retention periods
Immutable archiving
Location-independent work
Data access without VPN
Across companies
Multi-platform support
Data backup
Backup and data recovery
File versioning
Certified security
GDPR compliance
EuroPrise certificate
TeamDrive
KMU/Enterprise
Free choice of server
Choice of cloud, hybrid or on-premise
Hosted cloud services in Germany (SaaS)
Azure Cloud / OneDrive can be selected as storage locations
Integrations
External authentication (Azure AD)
Integrate directory services (AD, Shibboleth, OAuth 2.0)
Integrations into third-party systems (API)
Workflow Automation
Form integration (authorities/ healthcare/ orders)
Scan with Mobile Sandbox (Photos)
Connection of downstream systems
Individualization
Customization of external TeamDrive download pages
Customization of the inbox function
Individual email design
Security & Compliance
Monitoring retention periods
Unchangeable archives and logs (GoBD)
Immutable data storage (ransomware protection)
Users
Companies
Self-employed people
Freelancers
One-man businesses
Small and medium-sized enterprises (SMEs)
Healthcare
Doctor’s offices
Pharmacies
Laboratories
Clinics
Hospitals
Research institutes
Finance and Legal Affairs
Tax consultants
Lawyers
Notaries
Auditors
Authorities / public administration
Universities
School administrations
Municipalities
District offices
State parliaments
Government organizations
Non-profit organizations
Societies
Associations
Daycare centers
Churches and parish offices
Pupils and students
Industry
Automobile manufacturing
Mechanical engineering
Transport/Vehicle driver
Journalism
Publishers
Investigative journalism
Whistleblower
Other
Works councils
Recruiter
Compliance
EuroPriSe
Certification history
TeamDrive Cloud certificates
GDPR
Health Care – HIPAA
The German Hospital Federation
TISAX
ITAR
CCPA
Knowledge
TeamDrive FAQ
Video tutorials
TeamDrive in comparison
Cloud Computing
Backup
GDPR, ePrivacy
E-Invoicing
The German GOBD
Encyrption
Ransomware
Security by Design
Shop
Downloads
TeamDrive App
TeamDrive Outlook Add-In
TeamDrive Server App
TeamDrive Personal Server
TeamDrive manuals
Remote Support
Login
Web App
Search
Blog
FAQ
Contact
Data exchange.
Highly secure.
For self-employed people.
For small companies.
For large companies.
The TeamDrive cloud solution protects the data of
companies, authorities, organizations, law firms
and
associations
worldwide.
Get to know TeamDrive
from
6.33
€
*
Quickinfo
Tour
Video
GoBD – proper electronic accounting and archiving
Many companies already archive their documents and records electronically. However, there are requirements for the
digital storage
of relevant data which must be observed. One of these requirements is the principles for the proper management and storage of books, records and documents in electronic form and for data access (GoBD). We explain briefly and concisely the most important facts about the
proper accounting
and storage of electronic records.
Download Whitepaper for free
GoBD – what is that actually?
The
abbreviation GoBD
stands for the principles for the proper management and storage of books, records and documents in electronic form as well as for data access. It is an
administrative instruction
that was first issued by the Federal Ministry of Finance (BMF) in November 2014 and came into force during the same year. In November 2019, a BMF letter replaced the previous instruction. The ministry published a
new version
with numerous changes, which has been
valid since January 2020
. The decree of the tax authorities regulates the obligations for digital storage of tax-relevant data from accounting and business transactions.
To whom do the principles of the GoBD apply? They are
mandatory for all companies
. This is because the obligation regulated by law is not only binding for companies that have to present accounts, but also for small businesses, freelancers and the self-employed. If employees of the tax authorities find records and documents not recorded in
conformity with GoBD
during an operational audit, expensive consequences are imminent.
With the
introduction of the GoBD
, the previously obligatory principles of proper data processing supported accounting systems (GoBS) as well as the principles of data access and verifiability of digital documents (GDPdU) were summarized in an administrative instruction. In the old regulations it was regulated up to now that only enterprises are subject to the tax recording obligation if they also have to keep accounts. Only with the GoBD were other persons and companies included.
What needs to be done to fulfill the GoBD in the company?
The tax authorities demand audit-proof archiving from companies. Audit-proof procedural documentation or accounting means that all data subject to retention are excluded from
subsequent processing or manipulation
. This is particularly important if the documents are available in electronic form. The GoBD regulates these requirements precisely so that the accounting in the company remains traceable at all times for a tax consultant and the tax office.
Adaptation of the GoBD in 2020
In the new version of the GoBD, which has now been binding since January 2020, several
content has been supplemented
or made
more specific in the wording
. The reason for this was the rapid development of digital possibilities in recent years and the new electronic
solutions for bookkeeping
in companies that came along with it.
Therefore, among other things, it was reworded that
cloud systems
are now also suitable for the processing and storage of company documents and fulfill the requirements of IT-supported accounting systems. In addition, documents can now also be captured with the photo function of a smartphone and
stored in the cloud
. In addition, companies are no longer obliged to retain the
original paper documents
when filing electronically, as long as there is no change in content or important information is lost through conversion. Similarly, access for the tax authorities must not be restricted so that they can carry out their checks properly.
Six rules for tax-compliant accounting
1. Verifiability:
All postings in the company always follow the principle that
no posting is made without a receipt
. In addition, procedural documentation is required. This is because in the case of a tax audit, an external expert who is not involved in the internal control system must be able to obtain an overview of the business transactions and the situation of the company within a reasonable period of time.
2. Completeness:
According to the principle of the obligation to keep records of individual electronic invoices, every transaction in business operations must
be fully and completely
documented.
3. Timely and correct booking:
Another important point is the
timely recording
of business transactions. Financial transactions in cash must be recorded and booked within the same day. A period of ten days applies to cashless transactions. In addition to the time factor, the
correct documentation
of bookings also plays a major role. Only the actual circumstances in the business transactions may be represented.
4. Orderliness and immutability:
In the EDP
system bookings
are to be recorded systematically, so that by mechanical readability of the data also comprehensible results arise. The principles of clarity, unambiguousness and verifiability are applied.
Subsequent changes
must
be logged consistently
so that the original content can always be determined.
5. Security:
All electronic data must be protected against
unauthorized access
and also against
loss
.
6. Storage:
Electronically received documents and data are subject to a ten-year
retention period
. Business documents in the form of e-mails must be digitally archived for six to ten years. The form of the documents must be retained.
Obligation for procedural documentation
In addition to the principles of tax recording and retention of documents,
procedural documentation
must be established
. It helps to better check electronic accounting and describes the entire organizational and technical
process of archiving
. The following six steps belong to this process:
Creation (recording)
Indexing
Storage
Clear finding
Protection against loss and falsification
Reproduction of archived information
Retention periods of electronic documents
The
list
of electronic documents for which
retention periods
exist is long. The obligations to keep and not to change documents in IT-supported accounting include these proofs:
Accounting documents
Digital account books
Records of materials and merchandise management
Payroll accounting
Time Recording
Procedural documentation
These documents must be retained for
ten years
, while
different retention periods
apply for other documents, stacked records and business transactions according to the GoBD. The following list gives some examples:
commercial or business letters received
Reproduction of the commercial or business letters sent
other documents, insofar as they are relevant for taxation purposes
TeamDrive: GoBD-compliant software for document management
With the
TeamDrive software
, you can manage and archive your data and documents in an audit-proof manner. Our software enables companies to upload business
documents to the cloud
and store the data in an unalterable format. TeamDrive thus offers the possibility of
GoBD-compliant archiving
.
With each installation, TeamDrive Systems creates an RSA 2048/3072 key pair for confidential key exchange. All data is AES-256 encrypted before it is uploaded to the cloud. The keys remain with the user. With
end-to-end encryption
, only the user himself gains access to the unencrypted data.
The user creates a folder in which old documents can also be copied and backdated with the appropriate time of retention. A new version is saved with every change. An
indelible audit trail
guarantees the traceability of electronic archiving. Thus, our audit trail also replaces the manual process documentation.
For more detailed information, please request our GOBD
Whitepaper
.
Further knowledge on the subject area of the
German GOBD
GoBD
According to the
Principles of Proper Accounting
(GoBD)
, data and documents that are to be recognized by the tax authorities for
tax evidence
must be handled in a special way.
We will explain to you the most important facts about
archiving
and
storing electronic documents
.
Find out more about GoBD
Further knowledge in the areas of
data transfer
and
data storage
Cloud Computing
In the beginning,
cloud computing
was primarily understood to mean
the provision of storage volumes
via central data centers. Instead of buying storage, you could rent storage
flexibly and as needed
.
This continues to happen today in varying degrees, but the offering has been expanded to include numerous other interesting services from cloud providers.
Find out more about cloud computing
Backup
A backup is a backup copy of data that can be used to restore data if the original data is
damaged, deleted
or
encrypted
.
In the best case scenario, a backup should be stored in
a different location
than the original data itself -
ideally in a cloud
. You can find out why this is the case and what this has to do with
ransomware attacks
here.
Learn more about backup
GDPR, ePrivacy
With the introduction of the General Data Protection Regulation,
DSGVO
for short, extended requirements came into effect, especially with regard to
personal data protection
- including
sensitive sanctions
for violations of the law.
Read here what effects the GDPR has on you and your company.
The
ePrivacy Regulation
, which is still a work in progress at the moment, will also be discussed, but will in future formulate binding data protection rules that will apply
within the EU
.
Find out more about GDPR and ePrivacy
Encryption
In the digital age,
data protection
and
data security
play an outstanding role.
To ensure that electronic data cannot be viewed by third parties and to prevent data misuse, it must be encrypted. This applies both to their storage and, above all, to their transport via the public Internet.
You can get deeper insights into the topic of encryption here.
Learn more about encryption
Ransomware
Ransomware attacks
have increased significantly in recent years. After a successful attack, all data on your computer is
encrypted
. From this moment on you no longer have
any access
options. The economic damage to companies is often
enormous
.
Find out here how you can protect yourself against digital blackmail.
Learn more about ransomware
Security by Design
Especially with software that is intended to protect your users'' data from unauthorized access by third parties, software and data security must be taken into account and integrated into the
entire software life cycle.
You can find out why this is
very important
and how you as a user benefit from it here.
Find out more about Security by Design
About TeamDrive
Downloads
Partners & Resellers
Press
Vacancy
Whitepaper
Contact
FAQ
Support
Release Notes
Video tutorials
Imprint
Privacy Notice
GDPR Info for TeamDrive
Terms and conditions
Newsletter
Teamplace alternative
Boxcryptor alternative
Evaluation
© 2026 TeamDrive Systems GmbH
Page load link
Go to Top', '34e1a143756e6323eaf60c841a31740989a73e770087c0b950aeda57f4c39e73', '{"url": "https://teamdrive.com/en/knowledge/german-gobd/", "title": "GoBD - Principles for electronic accounting", "accessed_at": "2026-01-16T21:18:05.309951+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:18:05.310697+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (31, 'website', 'https://en.fileee.com/digitalisierung/gobd', 'GoBD briefly explained - fileee', NULL, 'GoBD briefly explained - fileee
SECURE THE BEST DEALS
TO THE OFFERS
NEW YEAR DEAL
20 % Sparen
// Code:
ORDNUNG26
HIER ENTDECKEN
SECURE THE BEST DEALS
TO THE OFFERS
NEW YEAR DEAL
20 % Sparen
// Code:
ORDNUNG26
HIER ENTDECKEN
Product
Features
All features at a glance
Pricing
fileee Spaces
fileee Appstore
fileee Partner
Products
fileeeBox
fileeeDIY
Solutions
Solutions
for you
for families
About fileee
About fileee
Our mission
Security
Sustainability
Artificial intelligence
Career with fileee
Resources
Info
Blog
Help centre
Become a partner
Private
Business
LOGIN
Register for free
Register for free
Product
Features
All features at a glance
Business pricing
fileee teams
fileee Appstore
fileee Partner
Products
fileeeBox
fileeeDIY
fileee Conversations
Solutions
Solutions
for self-employed
for clubs
for small businesses
for tax consultants
About fileee
About fileee
Our mission
Security
Sustainability
Artificial intelligence
Career with fileee
Resources
Info
Blog
Help centre
Media library
References
Become a partner
Digitization
Procedural documentation
NEW
E-bill
NEW
Digital document management
NEW
Digital personnel file
NEW
Digital invoice receipt
NEW
GoBD
Annual financial statements
Paperless office
Audit-proof archiving
Private
Business
LOGIN
Register for free
Register for free
Register for free
Private
Business
LOGIN
Product
Features
All features at a glance
Pricing
fileee Spaces
fileee Appstore
fileee Partner
Products
fileeeBox
fileee DIY
Solutions
Solutions
for you
for families
About fileee
About fileee
Our mission
Security
Sustainability
Artificial intelligence
Career with fileee
Resources
Info
Blog
Help centre
Become a partner
Register for free
Private
Business
LOGIN
Product
Features
All features at a glance
Pricing
fileee teams
fileee Appstore
fileee Partner
Products
fileee Conversations
fileeeBox
fileee DIY
Solutions
Solutions
for self-employed
for clubs
for small businesses
for tax consultants
Templates
No items found.
About fileee
About fileee
Our mission
Security
Sustainability
Artificial intelligence
Career with fileee
Resources
Info
Blog
Help centre
Media library
References
Become a partner
Digitization
Procedural documentation
NEW
E-bill
NEW
Digital document management
NEW
Digital personnel file
NEW
Digital invoice receipt
NEW
GoBD
Annual financial statements
Paperless office
Audit-proof archiving
We have updated our pricing
. Nothing has changed for private customers. Brand new: fileee Business
WATCH NOW
â
GoBD
The principles for the proper keeping and storage of books, records and documents in electronic form and for data access explained.
Definition of the GoBD
The abbreviation GoBD stands for: Principles for the proper keeping and storage of books, records and documents in electronic form and for data access. These principles, issued by the German Federal
Ministry of Finance
(BMF), contain criteria and guidelines that companies must fulfill when using electronic accounting.
GoBD briefly explained:
Requirements and background of the GoBD
As part of the GoBD, audit-proof storage describes the storage of digital data in terms of correctness, completeness, security, availability, traceability, immutability and access protection. These are to be understood as central requirements for audit security and are recorded in chapter three under "General requirements" of the GoBD guidelines:
Table of contents
What are the GoBD?
To whom do the GoBD apply?
Violations of the GoBD and consequences of non-compliance
Which documents are affected by the GoBD?
GoBD-compliant accounting with the help of a DMS
What else do I need to consider when complying with the GoBD?
Book free demo appointment
Since the demands on companies and IT-supported systems are becoming greater with increasing digitalization, the GoBD have also been adapted. In the latest version dated November 28, 2019, for example, topics such as
mobile scanning
or
cloud systems
are also included. A
mobile scan
is another import path into the system when capturing receipts.
However, the GoBD itself is not a legal text, but formally describes the criteria on the basis of which a tax audit takes place. What are the requirements and background of the GoBD? We explain briefly:
1. what are the GoBD?
Even if the
letter from the BMF
reads like a legal text, these principles formally "merely" set out criteria according to which it can be determined in a tax audit whether a company has complied with the proper keeping of books or records. Thus, compliance with the GoBD is in the hands of the taxpayer and the verification of this is in the hands of the tax office.
The GoBD describe the following points:
Use of GoBD-compliant software
Audit-proof archiving
Procedural documentation
GoBD-compliant operation
Important:
In this context, the retention periods specified in Section 147 (3) of the German Fiscal Code (AO) of 6 or 10 years must always be observed (to be found under Chapter 3, Item 27 of the GoBD Guidelines).
These four rules must be observed in digital archiving in order to be considered GoBD-compliant:
Inalterability, completeness, traceability and availability.
2 To whom do the GoBD apply?
All taxable entrepreneurs who generate profit income in any form are equally obliged to comply with the GoBD.
Based on the German Fiscal Code (Section 90 (3), items 141 to 144, AO ) and the individual tax laws (Section 22 UStG, Section 4 (3) sentence 5, Section 4 (4a) sentence 6, Section 4 (7) and Section 41 EStG), the GoBD obligate not only companies that are required to keep accounts but also self-employed persons, freelancers and small entrepreneurs who are not required to keep accounts to retain all tax-relevant data in accordance with the specified requirements.
Anyone who can ensure that the requirements anchored in the
GoBD
are met over the entire period of the retention periods is acting in
compliance with the GoBD
.
3. violations of the GoBD and consequences of non-compliance
In the event of non-compliance with the principles or violations of the GoBD, there are various consequences depending on the extent. If deficiencies in compliance with the GoBD are discovered during a tax audit and in particular in such a way that further deficiencies result from this, such as amounts being falsified or concealed as a result, this may have consequences under criminal tax law. As described above, the tax office proceeds according to the criteria of the GoBD during an audit and, depending on the extent, can, for example, demand tax arrears or interest on arrears, make estimates or even impose fines.
4. which documents are affected by the GoBD?
The GoBD concern all tax-relevant data. According to Â§ 147 para. 1 AO, these are as follows:
Books
Records
Inventories
Financial statements
Business and commercial letters
Important areas here are: Financial accounting, payroll accounting, cost accounting, bank accounts, asset accounting.
In addition, other documents are subject to retention and are therefore affected by the GoBD if they are relevant to the business. This also includes e-mails. E-mails must be archived and are subject to retention if the text of the e-mail contains relevant information for an invoice, for example. The e-mail does not have to be stored additionally if it only serves as a transmission medium and does not contain any relevant information.
5. GoBD-compliant accounting with the help of a DMS
The GoBD does not stipulate which system is to be used. Electronic archiving is primarily technology-neutral. However, it is advisable to use a DMS that is designed in accordance with the GoBD guidelines. The GoBD states: "The storage of data and electronic documents in a file system does not regularly meet the requirements of immutability unless additional measures are taken to ensure immutability" (GoBD chapter 8, point 110).
In addition to the criterion of immutability (GoBD chapter 3.2.5), the other criteria for
audit-proof archiving
must be met: Traceability and verifiability (GoBD chapter 3.1), completeness (GoBD chapter 3.2.1), accuracy (GoBD chapter 3.2.2), timely posting and records (GoBD chapter 3.2.3) and order (GoBD chapter 3.2.4). Here, audit-proof archiving acts as a prerequisite for GoBD-compliant archiving.
â
A DMS does not ensure audit compliance or compliance with the GoBD on its own, but can only be seen as an aid to implementation. The services of the various systems also differ.
â
Tip:
A comprehensive list of the requirements for a DMS in relation to GoBD compliance can be found at Bitkom in the "
GoBD checklist for document management systems
".
Best practice: How Winzerhof Wirges manages GoBD-compliant documents with fileee
: Digitizing the family business while complying with the GoBD guidelines - this was the challenge Andreas Wirges faced with his winery. In this webinar, we will show you how to quickly bring structure to your company''s bookkeeping and save valuable time while still complying with all legal requirements. Watch the
webinar now.
â
What else do I need to consider when complying with the GoBD?
In addition to the requirements for the DMS, the GoBD also regulates the requirements for the taxpayers, i.e. the internal recording and processing procedures as well as the behavior of the users.
The company itself is responsible for complying with the GoBD. This includes the following:
First, a digital system must be selected that meets the criteria of the GoBD with regard to digital filing.
Receipts must be captured in a timely manner (for example, via scan or import) and uploaded to the system. This can be done manually or automatically. It is important that the chronological sequence can be traced here as well. For a timely capture, one speaks of 8 to 10 days.
Likewise, procedural documentation must be created. This documentation must contain information on the data processing procedure: "The procedural documentation usually consists of a general description, user documentation, technical system documentation and operational documentation" (GoBD Chapter 10.1, item 153).
In addition, responsibilities must be clarified within the team: Who approves the data? Who checks the data?
The system itself is responsible for the availability and visibility of the necessary data, including history, as well as for
audit-proof archiving
and automatic creation of electronic files in terms of proper structure and coherence.
fileee BUSINESS
meets the requirements of the GoBD and supports your company in the audit-proof archiving of your electronic documents.
Any questions?
GoBD - Frequently asked questions
What does GoBD mean?
The GoBD are the "Principles for the proper keeping and storage of books, records and documents in electronic form as well as for data access" and were rewritten and defined by the Federal Ministry of Finance on November 28, 2019. These principles regulate the requirements to be met by digitally mapped processes from the perspective of the tax authorities.
What is revision security?
"Revision" means "alteration", "correction" or also "revision" and is understood in connection with "security", i.e. protection against it, in such a way that something is protected from change in this sense. In the context of documents, this term is used both in the technical and organizational area in the context of the electronic storage of data. The GoBD regulates the requirements for audit-proof storage.
What is the difference between GoBD and audit security?
The GoBD is the set of rules with the requirements for audit-proof storage and how audit security is given. Audit security is therefore a part of the GoBD and whoever acts in conformity with the GoBD, acts audit secure at the same time. In addition, the GoBD regulates, for example, the "data security" under the point 103 in chapter 7: "The taxpayer has to protect his DP system (...) against unauthorized inputs and changes (e.g. by access and access controls)". (from:
https://www.bundesfinanzministerium.de/Content/DE/Downloads/BMF_Schreiben/Weitere_Steuerthemen/Abgabenordnung/2019-11-28-GoBD.pdf?__blob=publicationFile&v=13)
What does GoBD compliance mean?
The implementation of the GoBD. Anyone who can ensure that the requirements anchored in the
GoBD
are met acts in
compliance with the GoBD
.
When is a DMS GoBD-compliant?
A DMS is GoBD-compliant if audit-proof storage is ensured and the GoBD requirements are met. The GoBD
checklist from Bitkom
breaks these down again directly for document management systems and explains the various points in connection with implementation.
All-around secure and GoBD-compliant with fileee Business
Start now with fileee Business
Book a demo
Get started straight away
Product
fileee Spaces
fileeeBox
fileee Appstore
fileee Partner
Solutions
Pricing
INFORMATION
Security
Sustainability
Artificial intelligence
Our Story
Our mission
Jobs
Magazine
SERVICE
Help centre
Support request
Login
DOWNLOAD
ABOUT FILEEE
Product
fileee Box
fileee Spaces
fileee Appstore
fileee Partner
Solutions
Pricing
INFORMATION
Security
Sustainability
Artificial intelligence
Document management
Our Story
Our mission
Jobs
Blog
SERVICE
Help centre
Support request
DOWNLOAD
Private
Business
Â© 2025 fileee. All Rights Reserved.
TOS
Performance specification
Data protection
Imprint
Product
fileee teams
fileeeBox
fileee Appstore
fileee Partner
fileee Conversations
Solutions
Pricing
INFORMATION
Security
Sustainability
Artificial intelligence
Document management
Our Story
Our mission
Jobs
Magazine
SERVICE
Help centre
Support request
Partner options
Login
DOWNLOAD
ABOUT FILEEE
Product
fileee Box
fileee teams
fileee Appstore
fileee Partner
Solutions
Pricing
INFORMATION
Security
Sustainability
Artificial intelligence
Our Story
Our mission
Jobs
Blog
SERVICE
Help centre
Support request
Partner options
DOWNLOAD
Private
Business
Â© 2025 fileee. All Rights Reserved.
TOS
Performance specification
Data protection
Imprint
Our website uses cookies
We at fileee want to offer you relevant content. For this purpose, we store information about your visit in so-called cookies.
              Click
here
if you only want to accept technically necessary cookies. Detailed information on data protection can be found
here
.
Agree', '52f2e4c1f117264f5101d0021b2cbb0bc25bdcdf2cfef0d29577069e4dd7e69b', '{"url": "https://en.fileee.com/digitalisierung/gobd", "title": "GoBD briefly explained - fileee", "accessed_at": "2026-01-16T21:18:05.593550+00:00", "status_code": 200, "content_type": "text/html"}', '2026-01-16T21:18:05.594263+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (32, 'website', 'https://www.aodocs.com/blog/gobd-explained-requirements-for-audit-ready-digital-bookkeeping-in-germany-and-beyond/', 'GoBD Explained: Requirements for Audit-Ready Digital ... - AODocs', NULL, 'GoBD Explained: Requirements for Audit-Ready Digital Bookkeeping in Germany and Beyond - AODocs
Skip to content
Products
Product
Document control
AI Process Automation
AI Assistant
Policies & Procedures
Legacy Replacement
Quality Management
Content Assembly
Record Management and Retention
Enterprise Apps
Google Workspace
Microsoft 365
SAP
View all integrations
Solutions
Industry
Manufacturing
Engineering and Construction
Energy
Healthcare
Professional Services
Banking and Insurance
Life Sciences
Line of Business
Human Resource Management
Legal
Finance & Procurement
Resources
Blog
Videos
Insights
Newsletter
Success Stories
Support
Migration Services
Implementation and Deployment Services
Knowledge Base
Status Page
Support Community
API Documentation
Company
About us
Careers
Contact
Contact us
Log in
Products
Product
Document control
AI Process Automation
AI Assistant
Policies & Procedures
Legacy Replacement
Quality Management
Content Assembly
Record Management and Retention
Enterprise Apps
Google Workspace
Microsoft 365
SAP
View all integrations
Solutions
Industry
Manufacturing
Engineering and Construction
Energy
Healthcare
Professional Services
Banking and Insurance
Life Sciences
Line of Business
Human Resource Management
Legal
Finance & Procurement
Resources
Blog
Videos
Insights
Newsletter
Success Stories
Support
Migration Services
Implementation and Deployment Services
Knowledge Base
Status Page
Support Community
API Documentation
Company
About us
Careers
Contact
GoBD Explained: Requirements for Audit-Ready Digital Bookkeeping in Germany and Beyond
August 15, 2025
Home
»
Blog
»
GoBD Explained: Requirements for Audit-Ready Digital Bookkeeping in Germany and Beyond
With Germany’s 2025 GoBD update tied to the e-invoicing mandate, businesses must now archive invoices in both human-readable and original XML formats. The shift reinforces GoBD’s core principles of immutability and audit-readiness—standards that may influence compliance thinking across Europe, much like SEC and FINRA rules shape recordkeeping in the U.S.
For companies operating in Germany—or with German business ties—the
GoBD
framework remains a key requirement: it governs how tax-relevant data must be recorded, retained, and protected. While not a law per se, GoBD carries legal weight—failure to comply can result in fines or audit complications.
The 2025 amendments
, introduced alongside Germany’s
e-invoicing mandate
, bring new clarity to how e-invoices must be stored. Businesses are now required to ensure that electronic invoices are archived in their original, machine-readable XML format, alongside any human-readable versions, and that the archived data meets the accessibility, and audit-readiness standards
defined in the GoBD
. Just as
SEC and FINRA
rules demand rigorous, audit-ready records for U.S. financial institutions, GoBD emphasizes
immutability, traceability, and structured recordkeeping
as core compliance principles.
Understanding GoBD
The
GoBD
(
Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff
) is an
administrative regulation from Germany’s Federal Ministry of Finance
. It clarifies obligations under Germany’s Fiscal Code (§§ 146 and 147 AO) concerning digital bookkeeping, record retention, and data access.
Key principles include:
Traceability and verifiability
of all entries and records
Immutability
— once data is recorded, it must remain unchanged
Timely and systematic recording
of business transactions
10-year retention
of tax-relevant digital documents
Documentation of IT systems and processes
Ensuring auditor access
to records when required
The 2025 update places special emphasis on
e-invoice archiving
: companies must store invoices in both human-readable and original structured data formats, maintain them in a compliant archive system, and ensure they can be accessed by auditors without alteration. GoBD applies to both digital and paper documents—especially when they are created, received, or stored electronically—and covers a comprehensive range of tax-relevant records, not just receipts.
Common Pitfalls in GoBD Compliance
Organizations often stumble when implementing GoBD, and the 2025 update introduces new challenges and risks tied to the e-invoicing mandate. Common pitfalls now include:
Incorrect e-invoice archiving
— storing only the human-readable PDF or print version, but not the original structured XML data required under the amended rules.
Storing records in formats that can’t be reproduced
exactly, compromising authenticity.
Failing to maintain version history or audit logs
, hindering traceability.
Inadequate enforcement of retention periods
or inconsistent deletion practices.
Scattered systems
with no centralized oversight or governance for audit readiness.
The e-invoicing changes mean companies can no longer treat invoice retention as a “PDF filing” exercise — both the
machine-readable source data
and any
human-friendly formats
must be preserved in compliance with immutability, accessibility, and auditability requirements. Businesses that overlook this are likely to fail an audit, even if other bookkeeping processes are sound.
Such issues can lead to audit challenges—or worse, penalties for noncompliance.
Building a Strong GoBD Compliance Framework
Compliance with GoBD—and safeguarding against audit complications—requires a methodical approach:
Immutable storage
to preserve original content and deter tampering
Centralized archives
to ensure consistency and governance across teams
Detailed audit trails
, capturing every access, change, and transfer
Automated retention policies
, aligned with statutory schedules
Secure, role-based access controls
, balancing security with usability
These steps help ensure records remain reliable, accessible, and defensible over time.
Why GoBD Is Worth Knowing Beyond Germany
GoBD often inspires similar thinking about data integrity across Europe, although it doesn’t carry direct force outside Germany. While it serves more as an example than a template, it can inform recordkeeping rigor expected in other jurisdictions. Being aware of these requirements—especially new rules on digital invoice archiving—is essential for companies that need to meet regulatory bookkeeping obligations across various territories and regulatory frameworks.
GoBD offers a concrete example of how strict digital recordkeeping principles may be applied in a regulatory context—especially regarding
immutability
,
auditability
, and long-term retention. Companies with operations in or connections to Germany must comply, and others may find it informative when evaluating or designing their own compliance frameworks.
While
some EU member states impose retention periods of
5–10 years
for tax-related records
, the details vary significantly by country. In that regard, while not a standard adopted elsewhere, GoBD can serve as a reference point.
How AODocs Compliance Archive Supports GoBD—and Similar Needs
Drawing on the same strengths that help
U.S. financial institutions comply with SEC and FINRA requirements
, AODocs’
Compliance Archive
is an effective solution for GoBD compliance:
Built-in
immutability features
, version control, and audit logging
Automated retention schedules
customized for German (and potentially other) regulatory periods
Centralized compliance dashboards
granting oversight across offices and functions
Seamless
integration with Google Workspace
, preserving workflows while enforcing compliance
AODocs helps organizations meet GoBD’s demands today, while staying adaptable for any evolving compliance standards across Europe or beyond.
Learn More:
How AODocs ensures FINRA compliance for U.S. financial institutions
Explore AODocs Compliance Archive solutions
Understanding how Google Workspace enables secure, compliant operations
SHARE:
Read next:
Blog
,
Compliance
,
Integrations
,
News & Announcements
,
SAP
AODocs Document Management Certified for SAP® Cloud ERP
AODocs’ AI-powered, cloud-native platform integrates with SAP to modernize document management, enhance security, and simplify compliance across ERP workflows. AODocs,...
December 16, 2025
Blog
,
AI
,
DMS
,
Knowledge Management
,
News & Announcements
AODocs Recognized in Gartner® Innovation Guide for Generative AI Knowledge...
AODocs announces it has been recognized as an Emerging Specialist in the Gartner® Innovation Guide for Generative AI Knowledge Management...
December 2, 2025
Blog
Beyond All-or-Nothing: Why Enterprise AI Agents Must Augment, Not Replace,...
The conversation around the rise of AI agents in work is often framed in black-and-white terms. An argument may sound...
December 2, 2025
Ready to get started?
See what AODocs can do for your company, let''s connect
Request a demo
Contact us
Linkedin
Youtube
Recognized by G2
Products
Document control
AI Assistant
Policies & Procedures
Legacy Replacement
Quality management
Content Assembly
Record Management and Retention
Google Workspace
Microsoft 365
SAP
Integrations
Document control
AI Assistant
Policies & Procedures
Legacy Replacement
Quality management
Content Assembly
Record Management and Retention
Google Workspace
Microsoft 365
SAP
Integrations
Solutions
Manufacturing
Engineering and Construction
Energy
Healthcare
Professional Services
Banking and Insurance
Life Sciences
Human Resource Management
Legal
Finance & Procurement
Manufacturing
Engineering and Construction
Energy
Healthcare
Professional Services
Banking and Insurance
Life Sciences
Human Resource Management
Legal
Finance & Procurement
Support
Migration Services
Implementation & Deployment Services
Knowledge base
Status page
Support community
API Documentation
Migration Services
Implementation & Deployment Services
Knowledge base
Status page
Support community
API Documentation
Resources
Blog
Videos
Insights
Newsletter
Success Stories
Blog
Videos
Insights
Newsletter
Success Stories
Company
About us
Careers
Contact us
About us
Careers
Contact us
Legal
Terms of Service
Professional Services Terms
Privacy Policy
Data Processing Agreement
Cookie Policy
Impressum
Terms of Service
Professional Services Terms
Privacy Policy
Data Processing Agreement
Cookie Policy
Impressum
Google disclosure
U.S. Patent 10,635,641
U.S. Patent 9,817,988
Copyright © 2012-2025 Altirnao Inc. All rights reserved.
We are using cookies to give you the best experience on our website.
You can find out more about which cookies we are using or switch them off in
settings
.
Accept
Close GDPR Cookie Settings
Privacy Overview
Strictly Necessary Cookies
Analytics
Powered by
GDPR Cookie Compliance
Privacy Overview
This website uses cookies so that we can provide you with the best user experience possible. Cookie information is stored in your browser and performs functions such as recognising you when you return to our website and helping our team to understand which sections of the website you find most interesting and useful.
Strictly Necessary Cookies
Strictly Necessary Cookie should be enabled at all times so that we can save your preferences for cookie settings.
Enable or Disable Cookies
Enabled
Disabled
Analytics
This website uses Google Analytics to collect anonymous information such as the number of visitors to the site, and the most popular pages.
Keeping this cookie enabled helps us to improve our website.
Enable or Disable Cookies
Enabled
Disabled
Enable All
Save Settings', 'ee5bfbb3e8111622bb132428f586c814cd176ca7c388510862be65d07aab183b', '{"url": "https://www.aodocs.com/blog/gobd-explained-requirements-for-audit-ready-digital-bookkeeping-in-germany-and-beyond/", "title": "GoBD Explained: Requirements for Audit-Ready Digital Bookkeeping in Germany and Beyond - AODocs", "accessed_at": "2026-01-16T21:18:05.900581+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:18:05.901322+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (33, 'website', 'https://www.comarch.com/trade-and-services/data-management/legal-regulation-changes/germany-updates-gobd-rules-to-reflect-mandatory-e-invoicing/', 'Germany Updates GoBD Rules to Reflect Mandatory E-Invoicing', NULL, 'Germany Updates GoBD Rules to Reflect Mandatory E-Invoicing
Products
BSS Products & Solutions
Comarch Business Banking
Comarch Custody
Comarch Digital Insurance
Comarch Insurance Claims
Comarch Factoring System
Comarch Life Insurance
Comarch Loan Origination
Comarch Wealth Management
Data Center & IT Services
e-Invoicing & EDI
E-learning
ERP-Schulungen
Free Resources
Intelligent Assurance & Analytics
IoT Connect
Loyalty Marketing Platform
OSS Products & Solutions
Training
Industries
Airlines & Travel
Finance, Banking & Insurance
Oil & Gas
Retail & Consumer Goods
Satellite Industry
Telecommunications
Utilities
Customers
Investors
About
About us
Awards
Comarch at a Glance
Comarch Group Companies
Corporate Social Responsibility
Management Board
Research & Development
Shareholders
Supervisory Board
Technology Partners & Industry Association
Contact
Contact a Consultant
Headquarters
Personal Data and Privacy Policy
Worldwide Offices
Press
News
Events
Comarch Telco Review blog
e-Invoicing Legal Updates
Loyalty Marketing Blog
Media Contact
Social media
Career
Partners
Language
EN
PL
EN
DE
FR
BE
NL
IT
ES
PT
JP
All categories
Other
Telecomunications
Finance
ERP
Large enterprises
Government
TV
Career
Investors
News and Events
Healthcare
Training
Field Service Management
Data Exchange
Customer Experience & Loyalty
ICT & Data Center
Language
EN
PL
EN
DE
FR
BE
NL
IT
ES
PT
JP
e-Invoicing & EDI
Solutions
AI-powered Data Management
e-Invoicing
Electronic Data Interchange
Global Compliance
Clients
Resources
Blog
Articles
Legal Updates
Vlogs: Digitalization Today
Events
Become a Partner
Contact us
Contact us
Comarch
Data Exchange & Document Management
Legal Regulation Changes
Get in touch!
Contact form
Partner Program
Newsletter
Germany Updates GoBD Rules to Reflect Mandatory E-Invoicing
Published
26 Aug 2025
Share
On July 14, 2025, the Federal Ministry of Finance (BMF) published the second amendment to the Principles for the Proper Management and Storage of Books, Records, and Documents in Electronic Form, and for Data Access (GoBD). The amendment, effective immediately, aligns the GoBD with Germany’s mandatory B2B e-invoicing regime, which entered into force on 1 January 2025.
Key Changes to the GoBD
Retention and Archiving Requirements
For e-invoices issued under Section 14(1) UStG, archiving the structured data component (e.g., XML file) is sufficient, provided all GoBD requirements are met.
A human-readable copy (such as a PDF) only needs to be stored if it contains additional tax-relevant information not included in the structured data.
Incoming electronic documents must be retained in the format received (e.g., invoices as XML, bank statements as PDF or image files).
For structured datasets, content conformity is required, but visual conformity is not.
Format conversions are allowed, but the original structured data must be preserved. If additional information is extracted (e.g., through OCR), the corrected data must also be retained.
Hybrid Invoices
For hybrid formats such as ZUGFeRD, the PDF component only needs to be archived if it contains different or additional tax-relevant details, such as accounting notes or electronic signatures.
Payment Processing Proofs
Technical proofs created by payment processors (e.g., logs) do not need to be stored unless they are used as accounting documents, are the only settlement record with the processor, or are the sole means of distinguishing cash from non-cash transactions.
Business Correspondence
Electronic business letters and accounting documents must be archived in the format in which they were received.
Audits and Data Access
Tax authorities may require a machine-readable evaluation of retained data or have it carried out by an authorized third party. Taxpayers must provide data in a processable export format or grant read-only access.
VAT-Related Changes
Alongside the GoBD update, Germany has also updated its VAT invoicing and archiving requirements to mandate B2B e-invoicing from January 1, 2025, in
structured XML formats
as the legal record, as well as reduced the statutory retention period for invoices
from 10 years to 8 years
. It also introduced
revised credit note
rules (effective 6 December 2024), under which a credit note with VAT issued by a non-entrepreneur may trigger unauthorized tax liability if not promptly objected to by the recipient.
There’s more you should know about
e-invoicing in Germany
–
learn more about the new and upcoming regulations.
Other news
08 Jan 2026
Sri Lanka Launches National E-Invoicing System to Modernize Tax Infrastructure
08 Jan 2026
Spain Approves Postponement of the Veri*factu Implementation
08 Jan 2026
Slovakia Approves Mandatory E-Invoicing and Reporting Framework
07 Jan 2026
Poland Finalizes Legal Framework for Mandatory KSeF 2.0
05 Jan 2026
Poland Publishes the Official List of KSeF E-Invoicing Exemptions
How Can We Help?
💬
Compliance issues? Supply chain trouble? Integration challenges? Let’s chat.
Schedule a discovery call
Newsletter
Expert Insights on
Data Exchange
We always check our sources – so, no spam from us.
Sign up to start receiving:
legal news
expert materials
event invitations
Please wait
Data Exchange & Document Management
AI-powered Data Management
e-Invoicing
Global Compliance
E-Invoicing in Poland (KSeF)
Electronic Data Interchange (EDI)
About
Data Management News
Legal Regulation Changes
Data Management Events
Vlogs - Digitalization Today
What''s openPEPPOL?
Resources
Client & Success Stories
Glossary
Other Products & Services
Loyalty Marketing Platform
Information and Communication Technologies
Contact
info@comarch.com
Contact form
Partner Program
Newsletter
Follow us on
linkedin
Follow us on
youtube
Comarch Group
Home
About the Comarch Group
Other Industries
Comarch Group Customers
Investors
Copyright © 2015 - 2026 Comarch SA. All rights reserved.
Personal Data and Privacy Policy
|
Cookie settings
Q6HKO63D8VIE0IKVC5J3', '577906de8227791b044db686e46b39fc465a59c0d5558e5e5e490eced8edbbb3', '{"url": "https://www.comarch.com/trade-and-services/data-management/legal-regulation-changes/germany-updates-gobd-rules-to-reflect-mandatory-e-invoicing/", "title": "Germany Updates GoBD Rules to Reflect Mandatory E-Invoicing", "accessed_at": "2026-01-16T21:18:06.168744+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:18:06.169901+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (34, 'website', 'https://externer-datenschutzbeauftragter-dresden.de/en/data-protection/dsgvo-vs-gobd-all-entrepreneurs-criminal/', 'DSGVO vs GoBD - All entrepreneurs criminal?', NULL, 'DSGVO vs GoBD - All entrepreneurs criminal?
035179593513
datenschutz@externer-datenschutzbeauftragter-dresden.de
Facebook
LinkedIn
Twitter
0 Items
Start
Services
Prices
Offer
Promotion
Contact
Blog
Store
Appointment of external DPO
Online data protection audit
SME backup consulting
Online data protection advice
Trainings
F-Secure
Sophos
Tools
Anonymizer
Vulnerabilities scan
Transcription
Whistleblower Reporting Center
GDPR Scanner
Automatic e-mail phishing detection
Whistleblower
other
Information Security
IT
DSMS
Data protection documentation
Data loss questionnaire
Information sheet: Offboarding of employees - data protection and information security
Select Page
DSGVO vs GoBD - All entrepreneurs criminal?
by
Tessa Lamken
|
Jul 3, 2022
|
Privacy
The GoBD (principles for the proper keeping and storage of books, records and documents in electronic form), like the DSGVO, contains storage obligations. Which ones do companies have to adhere to in order to avoid fines or similar? Does the retention obligation from the GoBD possibly even contradict the GDPR?
What is the GoBD?
GoBD is an abbreviation for "Principles for the proper keeping and storage of books, records and documents in electronic form". This is an administrative instruction from the Ministry of Finance that came into force in 2014 and was revised at the beginning of 2020. It contains basic principles that entrepreneurs must follow for their books and other records. The purpose is to have these documents recognized by the tax authorities for tax evidence purposes. All companies are affected by these regulations, regardless of their size.
The GoBD only regulates the basic principles of retention and storage. However, it does not regulate which documents are to be retained at all and how long they are to be retained. However, this results from other laws.
Which storage obligation does the GoBD regulate?
According to the GoBD, everything that is of significance for the taxation of the company must be documented and stored. For this purpose, it contains the principle of traceability and verifiability. In addition, the principles of truth, clarity and continuous recording are also found here (these contain the principles of completeness, accuracy, timely document backup, order and immutability).
Accordingly, all postings must be accompanied by a receipt. This must be up-to-date and correct. These must be subject to audit compliance, i.e. the postings must be recorded systematically and the vouchers and records must not be altered. This procedure requires a
Procedure directory
to keep. All of this must also be archived.
Contradiction to the GDPR?
In practice, the question often arises as to whether the provisions on storage and archiving under the GoBD contradict the GDPR. If this were the case, all companies that follow the requirements of the GoBD,
contrary to data protection
act.
The core of the problem is that the documents to be stored in accordance with the GoBD are often
contain personal data and therefore fall within the scope of the GDPR
fall. The GDPR itself regulates storage limitations until the end of the purpose (Art. 5 GDPR). Does the GoBD contradict this regulation?
The answer is clear: No! For the
Storage according to the GoBD are legal deadlines
must be observed. This applies regardless of whether the document to be retained contains personal data or not. During this retention period, there is a purpose, namely the tax law purpose of the GoBD. If the deadline under the GoBD expires, the purpose of data processing within the meaning of the GDPR also ceases to apply and the data must be deleted. An appropriate deletion concept is important here.
A breach of data protection law only occurs if personal data is stored beyond the statutory retention period or if the concept of
GoBD
is used as a cover for the unauthorized storage of data.
Conclusion
The retention obligation under the GoBD does not contradict the GDPR. Businesses that comply with this
Storage and the corresponding legal deadlines
do not act in breach of data protection regulations. If the data to be retained contains personal data, the GDPR and its basic principles must also be observed within the framework of the GoBD. Once the retention obligations have expired, the data must be deleted in accordance with data protection regulations.
Our team of experts will be happy to advise you on all topics relating to
Data protection and data security!
Blog posts
Developing data protection strategies: long-term protection for companies
Data breaches: How to react correctly
External data protection officer: tasks, duties and qualifications
Email archiving GDPR: Mandatory or optional?
AI law: regulation of artificial intelligence in the EU
The 4 protection goals of data protection: confidentiality, integrity, availability and usability
External data protection officer for SMEs: How to find the right partner
Checking and securing data protection practices with a data protection audit
Search
Search
History Blog
History Blog
Select Month
November 2025  (1)
June 2025  (10)
May 2025  (10)
April 2025  (6)
March 2025  (16)
January 2025  (14)
December 2024  (12)
November 2024  (16)
October 2024  (13)
September 2024  (14)
August 2024  (4)
July 2024  (13)
June 2024  (5)
May 2024  (8)
April 2024  (1)
February 2024  (31)
January 2024  (69)
December 2023  (2)
September 2023  (3)
August 2023  (2)
July 2023  (1)
June 2023  (1)
March 2023  (1)
January 2023  (3)
December 2022  (6)
November 2022  (2)
October 2022  (4)
September 2022  (13)
August 2022  (20)
July 2022  (2)
June 2022  (4)
May 2022  (4)
April 2022  (4)
March 2022  (3)
February 2022  (3)
January 2022  (2)
December 2021  (5)
November 2021  (9)
October 2021  (8)
September 2021  (7)
August 2021  (5)
July 2021  (8)
June 2021  (7)
May 2021  (6)
April 2021  (3)
November 2019  (2)
March 2019  (2)
February 2019  (1)
December 2018  (1)
September 2018  (10)
August 2018  (16)
July 2018  (19)
May 2018  (1)
April 2018  (1)
February 2018  (1)
Tags
Doctor''s visit
(4)
Storage
(8)
Retention periods
(9)
Images
(4)
Cyber attack
(8)
Cyberattacks
(6)
Cybercrime
(9)
Cybersecurity
(14)
Data on the phone
(7)
Data breach
(22)
Privacy
(166)
Data Protection Officer
(42)
Data Protection Act
(5)
Data protection in the company
(46)
Data protection in the company
(66)
Data protection in the association
(6)
Privacy Travel
(5)
Privacy policy
(10)
Data protection breach
(18)
Data security
(35)
Data processing
(4)
Digitization
(4)
Discretion in the waiting room
(6)
DPO
(30)
GDPR
(25)
EU General Data Protection Regulation
(37)
External data protection officer
(13)
Photography
(5)
Secret data
(23)
Health data
(11)
Information Security
(5)
Internal
(4)
Internal data protection officer
(9)
IT Security
(18)
Artificial intelligence
(5)
Patient data
(6)
Patient data protection
(7)
Personnel data
(8)
personal data
(39)
Personal data
(17)
Privacy
(8)
Right to deletion
(6)
Confidentiality
(4)
Telephone advertising
(5)
Company Privacy
(24)
Services
Store
Information Security
Contact
Blog
Imprint
Privacy policy
Payment methods
Shipping methods
Cancellation policy
AGB
Checkout
Shopping cart
License conditions of the online trainings
Cookie Directive (EU)
Facebook
X
Instagram
RSS
DATUREX GmbH
DSB buchen
We''ve detected you might be speaking a different language. Do you want to change to:
Deutsch
Deutsch
English
Español
Français
Polski
Change Language
Close and do not switch language
Manage cookie consent
We use cookies to optimize our website and service.
Functional
Functional
Always active
Technical storage or access is strictly necessary for the lawful purpose of enabling the use of a particular service expressly requested by the subscriber or user, or for the sole purpose of carrying out the transmission of a message over an electronic communications network.
Preferences
Preferences
The technical storage or access is necessary for the legitimate purpose of storing preferences that have not been requested by the subscriber or user.
Statistics
Statistics
The technical storage or access, which is carried out exclusively for statistical purposes.
Technical storage or access used solely for anonymous statistical purposes. Without a subpoena, the voluntary consent of your Internet service provider, or additional records from third parties, information stored or accessed for this purpose alone generally cannot be used to identify you.
Marketing
Marketing
Technical storage or access is necessary to create user profiles, to send advertisements, or to track the user on a website or across multiple websites for similar marketing purposes.
Manage options
Manage services
Manage {vendor_count} vendors
Read more about these purposes
Accept
Decline
Preferences
Save settings
Preferences
{title}
{title}
{title}
Manage consent
English
Deutsch
Español
Français
Polski', '2c6bc7356273a4ac50b354d8ef5632eb80c709a54f12f29beba0145f0ad2d2c7', '{"url": "https://externer-datenschutzbeauftragter-dresden.de/en/data-protection/dsgvo-vs-gobd-all-entrepreneurs-criminal/", "title": "DSGVO vs GoBD - All entrepreneurs criminal?", "accessed_at": "2026-01-16T21:18:26.665326+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:18:26.666093+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (35, 'website', 'https://gdpr.eu/what-is-gdpr/', 'What is GDPR, the EU''s new data protection law?', NULL, 'What is GDPR, the EU’s new data protection law? - GDPR.eu
Facebook
Twitter
Search
Search
Home
Checklist
FAQ
GDPR
News & Updates
What is GDPR, the EU’s new data protection law?
What is the GDPR? Europe’s new data privacy and security law includes hundreds of pages’ worth of new requirements for organizations around the world. This GDPR overview will help you understand the law and determine what parts of it apply to you.
The
General Data Protection Regulation (GDPR)
is the toughest privacy and security law in the world. Though it was drafted and passed by the European Union (EU), it imposes obligations onto organizations anywhere, so long as they target or collect data related to people in the EU. The regulation was put into effect on May 25, 2018. The GDPR will levy harsh fines against those who violate its privacy and security standards, with penalties reaching into the tens of millions of euros.
With the GDPR, Europe is signaling its firm stance on data privacy and security at a time when more people are entrusting their personal data with cloud services and breaches are a daily occurrence. The regulation itself is large, far-reaching, and fairly light on specifics, making GDPR compliance a daunting prospect, particularly for small and medium-sized enterprises (SMEs).
We created this website to serve as a resource for SME owners and managers to address specific challenges they may face. While it is not a substitute for legal advice, it may help you to understand where to focus your GDPR compliance efforts. We also offer tips on
privacy tools
and how to mitigate risks. As the GDPR continues to be interpreted, we’ll keep you up to date on evolving best practices.
If you’ve found this page — “what is the GDPR?” — chances are you’re looking for a crash course. Maybe you haven’t even found the document itself yet (tip:
here’s the full regulation
). Maybe you don’t have time to read the whole thing. This page is for you. In this article, we try to demystify the GDPR and, we hope, make it less overwhelming for SMEs concerned about GDPR compliance.
History of the GDPR
The right to privacy is part of the 1950
European Convention on Human Rights
, which states, “Everyone has the right to respect for his private and family life, his home and his correspondence.” From this basis, the European Union has sought to ensure the protection of this right through legislation.
As technology progressed and the Internet was invented, the EU recognized the need for modern protections. So in 1995 it passed the European Data Protection Directive, establishing minimum data privacy and security standards, upon which each member state based its own implementing law. But already the Internet was morphing into the data Hoover it is today. In 1994, the
first banner ad
appeared online. In 2000, a majority of financial institutions offered online banking. In 2006, Facebook opened to the public. In 2011, a Google user sued the company for scanning her emails. Two months after that, Europe’s data protection authority declared the EU needed “a comprehensive approach on personal data protection” and work began to update the 1995 directive.
The GDPR entered into force in 2016 after passing European Parliament, and as of May 25, 2018, all organizations were required to be compliant.
Scope, penalties, and key definitions
First, if you process the personal data of EU citizens or residents, or you offer goods or services to such people, then
the GDPR applies to you even if you’re not in the EU
. We talk more about this
in another article
.
Second, the
fines for violating the GDPR are very high
. There are two tiers of penalties, which max out at €20 million or 4% of global revenue (whichever is higher), plus data subjects have the right to seek compensation for damages. We also talk
more about GDPR fines
.
The GDPR defines an array of legal terms at length. Below are some of the most important ones that we refer to in this article:
Personal data
— Personal data is any information that relates to an individual who can be directly or indirectly identified. Names and email addresses are obviously personal data. Location information, ethnicity, gender, biometric data, religious beliefs, web cookies, and political opinions can also be personal data.
Pseudonymous
data can also fall under the definition if it’s relatively easy to ID someone from it.
Data processing
— Any action performed on data, whether automated or manual. The examples cited in the text include collecting, recording, organizing, structuring, storing, using, erasing… so basically anything.
Data subject
— The person whose data is processed. These are your customers or site visitors.
Data controller
— The person who decides why and how personal data will be processed. If you’re an owner or employee in your organization who handles data, this is you.
Data processor
— A third party that processes personal data on behalf of a data controller. The GDPR has special rules for these individuals and organizations. These could include cloud servers, like
Google Drive
,
Proton Drive
, or
Microsoft OneDrive,
or email service providers, like
Proton Mail
.
What the GDPR says about…
For the rest of this article, we will briefly explain all the key regulatory points of the GDPR.
Data protection principles
If you process data, you have to do so according to seven protection and accountability principles outlined in
Article 5.1-2
:
Lawfulness, fairness and transparency
— Processing must be lawful, fair, and transparent to the data subject.
Purpose limitation
— You must process data for the legitimate purposes specified explicitly to the data subject when you collected it.
Data minimization
— You should collect and process only as much data as absolutely necessary for the purposes specified.
Accuracy
— You must keep personal data accurate and up to date.
Storage limitation
— You may only store personally identifying data for as long as necessary for the specified purpose.
Integrity and confidentiality
— Processing must be done in such a way as to ensure appropriate security, integrity, and confidentiality (e.g. by using encryption).
Accountability
— The data controller is responsible for being able to demonstrate GDPR compliance with all of these principles.
Accountability
The GDPR says data controllers have to be able to demonstrate they are GDPR compliant. And this isn’t something you can do after the fact: If you think you are compliant with the GDPR but can’t show how, then you’re not GDPR compliant. Among the ways you can do this:
Designate data protection responsibilities to your team.
Maintain detailed documentation of the data you’re collecting, how it’s used, where it’s stored, which employee is responsible for it, etc.
Train your staff and implement technical and organizational security measures.
Have Data Processing Agreement contracts in place with third parties you contract to process data for you.
Appoint a Data Protection Officer (though not all organizations need one — more on that in
this article
).
Data security
You’re required to handle data securely by implementing “
appropriate technical and organizational measures
.”
Technical measures mean anything from requiring your employees to use
two-factor authentication
on accounts where personal data are stored to contracting with cloud providers that use
end-to-end encryption
.
Organizational measures are things like
staff trainings
, adding a
data privacy policy
to your employee handbook, or
limiting access to personal data
to only those employees in your organization who need it.
If you have a data breach, you have 72 hours to tell the data subjects or face penalties. (This notification requirement may be waived if you use technological safeguards, such as encryption, to render data useless to an attacker.)
Data protection by design and by default
From now on, everything you do in your organization must, “by design and by default,” consider data protection. Practically speaking, this means you must consider the data protection principles in the design of any new product or activity. The GDPR covers this principle in
Article 25
.
Suppose, for example, you’re launching a new app for your company. You have to think about what personal data the app could possibly collect from users, then consider ways to minimize the amount of data and how you will secure it with the latest technology.
When you’re allowed to process data
Article 6
lists the instances in which it’s legal to process person data. Don’t even think about touching somebody’s personal data — don’t collect it, don’t store it, don’t sell it to advertisers — unless you can justify it with one of the following:
The data subject gave you specific,
unambiguous consent
to process the data. (e.g. They’ve opted in to your marketing email list.)
Processing is necessary to execute or to prepare
to enter into a contract
to which the data subject is a party. (e.g. You need to do a background check before leasing property to a prospective tenant.)
You need to process it
to comply with a legal obligation
of yours. (e.g. You receive an order from the court in your jurisdiction.)
You need to process the data
to save somebody’s life
. (e.g. Well, you’ll probably know when this one applies.)
Processing is necessary
to perform a task in the public interest
or to carry out some official function. (e.g. You’re a private garbage collection company.)
You have a
legitimate interest
to process someone’s personal data. This is the most flexible lawful basis, though the “fundamental rights and freedoms of the data subject” always override your interests, especially if it’s a child’s data. (It’s difficult to give an example here because there are a variety of factors you’ll need to consider for your case. The UK Information Commissioner’s Office provides helpful guidance
here
.)
Once you’ve determined the lawful basis for your data processing, you need to document this basis and notify the data subject (transparency!). And if you decide later to change your justification, you need to have a good reason, document this reason, and notify the data subject.
Consent
There are strict new rules about what constitutes
consent from a data subject
to process their information.
Consent must be “freely given, specific, informed and unambiguous.”
Requests for consent must be “clearly distinguishable from the other matters” and presented in “clear and plain language.”
Data subjects can withdraw previously given consent whenever they want, and you have to honor their decision. You can’t simply change the legal basis of the processing to one of the other justifications.
Children under 13 can only give consent with permission from their parent.
You need to keep documentary evidence of consent.
Data Protection Officers
Contrary to popular belief, not every data controller or processor needs to appoint a
Data Protection Officer (DPO)
. There are three conditions under which you are required to appoint a DPO:
You are a public authority other than a court acting in a judicial capacity.
Your core activities require you to monitor people systematically and regularly on a large scale. (e.g. You’re Google.)
Your core activities are large-scale processing of special categories of data listed under
Article 9
of the GDPR or data relating to criminal convictions and offenses mentioned in
Article 10
. (e.g. You’re a medical office.)
You could also choose to designate a DPO even if you aren’t required to. There are benefits to having someone in this role. Their basic tasks involve understanding the GDPR and how it applies to the organization, advising people in the organization about their responsibilities, conducting data protection trainings, conducting audits and monitoring GDPR compliance, and serving as a liaison with regulators.
We go in depth about the DPO role
in another article
.
People’s privacy rights
You are a data controller and/or a data processor. But as a person who uses the Internet, you’re also a data subject. The GDPR recognizes a litany of new
privacy rights for data subjects
, which aim to give individuals more control over the data they loan to organizations. As an organization, it’s important to understand these rights to ensure you are GDPR compliant.
Below is a rundown of data subjects’ privacy rights:
The right to be informed
The right of access
The right to rectification
The right to erasure
The right to restrict processing
The right to data portability
The right to object
Rights in relation to automated decision making and profiling.
Conclusion
We’ve just covered all the major points of the GDPR in a little over 2,000 words. The
regulation itself
(not including the accompanying directives) is 88 pages. If you’re affected by the GDPR, we strongly recommend that someone in your organization reads it and that you consult an attorney to ensure you are GDPR compliant.
Related Posts
Art. 68 GDPR - European Data Protection Board
Art. 39 GDPR - Tasks of the data protection officer
Art. 38 GDPR - Position of the data protection officer
Forms and Templates
Data Processing Agreement
Right to Erasure Request Form
Privacy Policy
Ben Wolford
Editor in Chief, GDPR EU
A journalist by training, Ben has reported and covered stories around the world. He joined
Proton
to help lead the fight for data privacy.
About GDPR.EU
GDPR.EU is a website operated by Proton AG, which is co-funded by Project REP-791727-1 of the Horizon 2020 Framework Programme of the European Union. This is not an official EU Commission or Government resource. The europa.eu webpage concerning GDPR can be found
here
. Nothing found in this portal constitutes legal advice.
Getting Started
What is GDPR?
What are the GDPR Fines?
GDPR Compliance Checklist
Templates
Data Processing Agreement
Right to Erasure Request Form
Writing a GDPR-compliant privacy notice
Technical Review
Data Protection Office Guide
GDPR and Email
Does GDPR apply outside of the EU
About Us
GDPR.eu is co-funded by the
Horizon 2020
Framework Programme of the European Union
and operated by Proton AG
.
GDPR Forms and Templates
Data Processing Agreement
Right to Erasure Request Form
Privacy Policy
© 2026 Proton AG. All Rights Reserved.
Terms and Conditions
Privacy Policy
GDPR compliance is easier with
encrypted email
Learn more', '9a1ff205965da8ec043854e81032ae521f88ad5d9fd0757da7677d2e62a3c7d7', '{"url": "https://gdpr.eu/what-is-gdpr/", "title": "What is GDPR, the EU’s new data protection law? - GDPR.eu", "accessed_at": "2026-01-16T21:18:26.850969+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:18:26.853720+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (36, 'website', 'https://www.lexology.com/library/detail.aspx?g=19c9005b-3c4f-4a48-b585-90fa69c758c9', 'What You Need to Know About the GoBD - Lexology', NULL, 'What You Need to Know About the GoBD - Lexology
Skip to content
Search Lexology
PRO
Events
Awards
Explore
Login
Register
Home
Latest intelligence
Legal research
Regulatory monitoring
Practical resources
Experts
Learn
Awards
Influencers
Lexology Index Awards 2025
Lexology European Awards 2026
Client Choice Dinner 2026
Lexology Compete
About
Help centre
Blog
Lexology Academic
Login
Register
PRO
Compete
Lexology
Article
﻿
Back
Forward
Save & file
View original
Forward
Print
Share
Facebook
Twitter
LinkedIn
WhatsApp
Follow
Please login to follow content.
Like
Instruct
add to folder:
My saved
(default)
Read later
Folders shared with you
Register now
for your free, tailored, daily legal newsfeed service.
Find out more about Lexology or get in touch by visiting our
About
page.
Register
What You Need to Know About the GoBD
Association of Corporate Counsel
Germany
,
Global
,
USA
September 11 2017
Authored by:
K Royal,
technology columnist for www.AccDocket.com, and vice president, associate general counsel of privacy, and compliance/privacy officer at CellTrust Corp.
This article was published as part of ACC’s “This Week in Privacy” series, a new column for in-house counsel who need advice in the privacy and cybersecurity sectors.
Question:
With all the data protection reform going on in Europe, I heard about something called the GoBD, which pertains to tax papers. What is that?
Answer:
Unlike the General Protection Data Regulation (GDPR), the GoBD is not a well-known or oft-discussed topic. The German GoBD, or the “basic principles on the proper keeping and storage of financial books, recordings, and documents in electronic form as well as data access” (Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff), became effective a little over two years ago and is specifically related to tax documentation. It replaced two prior requirements: one from 1995, the GoBS (principles of proper DV-based accounting systems), and one from 2001, the GDPdU (principles of data access and verifiability of digital documents).
The GoBD greatly increases the reach of the German Ministry of Finance, because not only are there many types of documents, records, and data that can be linked to tax purposes, but also because the Ministry requires a years’ worth of continuous documentation. The documentation is especially critical in cash-based businesses, like hair salons and restaurants, because cash transactions is highly subject to manipulation and inaccurate reporting.
In this digital age, many documents and records are created or retained electronically. Some records are still required to be kept in original paper, such as donation receipts and capital gains certificates. Otherwise, companies often desire to reduce the paper burden and retain digitized copies.
The GoBD facilitates that desire, but requires that the auditability and traceability of the original transactions remain. For example, a PDF/A-3 comprises both an image and XML filed linked to the information contained in the image. The tx authorities would need to be able to audit that electronic file. If it is transformed into a JPG, TNG, or PNG, then the XML information would be lost.
The GoBD also contains timeframe restrictions — cash transactions must be captured daily and non-cash transactions must be captured every 10 days. Certain transactions are permitted to be captured on a monthly basis, but there are limitations and requirements around regular scheduling of these digitization actions. The two specific provisions in the GoBD around electronic record-keeping are data immutability and security.
For more guidance on the GoBD, please visit one of the following links:
VGD
,
SMACC
, or
Bundesministerium der Finanzen
.
For further reading, download
ACC’s White Paper on “What Every GC Needs to Know About Third Party Cyber Diligence
.”
The Association of Corporate Counsel (ACC) is a global legal association that promotes the common professional and business interests of in-house counsel who work for corporations, associations and other private-sector organizations through information, education, networking opportunities and advocacy initiatives. With more than 45,000 members in 85 countries, employed by over 10,000 organizations, ACC connects its members to the people and resources necessary for both personal and professional growth. By in-house counsel, for in-house counsel.®
﻿
Back
Forward
Save & file
View original
Forward
Print
Share
Facebook
Twitter
LinkedIn
WhatsApp
Follow
Please login to follow content.
Like
Instruct
add to folder:
My saved
(default)
Read later
Folders shared with you
Filed under
Germany
Global
USA
IT & Data Protection
Law Department Management
Tax
Association of Corporate Counsel
Topics
Information privacy
Laws
GDPR
Interested in contributing?
Get closer to winning business faster with Lexology''s complete suite of dynamic products designed to help you unlock new opportunities with our highly engaged audience of legal professionals looking for answers.
Learn more
Professional development
Implementing & Maintaining Data Retention & Data Management Policies - Learn Live
MBL Seminars | 1.5 CPD hours
Online
18 March 2026
Mastering Data Processing Agreements - Drafting, Negotiating & Mitigating Risk- Learn Live
MBL Seminars | 4 CPD hours
Online
12 May 2026
Microsoft Outlook - Going Beyond the Basics - Learn Live
MBL Seminars | 2 CPD hours
Online
20 January 2026
View all
Related practical resources
PRO
How-to guide
How-to guide: How to deal with a GDPR data breach (UK)
How-to guide
How-to guide: How to establish a valid lawful basis for processing personal data under the GDPR (UK)
How-to guide
How-to guide: How to ensure compliance with the GDPR (UK)
View all
Related research hubs
GDPR
Germany
USA
IT & Data Protection
Tax
Resources
Daily newsfeed
Panoramic
Research hubs
Learn
In-depth
Lexy: AI search
Scanner
Contracts & clauses
Lexology Index
Find an expert
Reports
Research methodology
Submissions
FAQ
Instruct Counsel
Client Choice 2025
More
About us
Legal Influencers
Firms
Blog
Events
Popular
Lexology Academic
Legal
Terms of use
Cookies
Disclaimer
Privacy policy
Contact
Help centre
Contact
RSS feeds
Submissions
Login
Register
Follow on X
Follow on LinkedIn
© Copyright 2006 -
2026
Law Business Research', 'c988ea6fa8275d028643b25f711ac3f523bdd56188b67099d458676a1eb44de3', '{"url": "https://www.lexology.com/library/detail.aspx?g=19c9005b-3c4f-4a48-b585-90fa69c758c9", "title": "What You Need to Know About the GoBD - Lexology", "accessed_at": "2026-01-16T21:18:27.252054+00:00", "status_code": 200, "content_type": "text/html; charset=utf-8"}', '2026-01-16T21:18:27.252748+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (37, 'website', 'https://rtcsuite.com/germany-clarifies-e-invoice-archiving-rules-gobd-2025-amendment-how-businesses-must-now-store-einvoices/', 'Germany Clarifies E-Invoice Archiving Rules: GoBD 2025 Amendment', NULL, 'Germany Clarifies E-Invoice Archiving Rules: GoBD 2025 Amendment: How Businesses Must Now Store EInvoices  - RTC Suite
Cloud Platform
RTC Suite
Architecture
Benefits & Features
SAP BTP Cockpit
ERP Integration
SAP
Oracle
MS Dynamics
Other ERP
Solutions
Digital Reporting Requirements (DRR)
e-Invoicing
Invoice Reporting
ViDA (VAT in the Digital Age)
e-Waybill
Reporting
SAF-T
VAT Return
CbCR (Country by Country reports)
Intrastat Reports
Plastic Tax Reports
EC Sales List
Automation
AP Automation
e-Banking
Reconciliation
Partners
Partnership Beyond Technology
Referral Partners
Implementation Partners
Technology Alliances
Strategic Partners
Media Partners
Partnership Benefits
Become a Partner
About Us
Company
Our Story
Our Leadership
Data Privacy & Security
Quality & Service
Awards & Certificates
Global Presence
Interoperability Framework
RTC Offices
Career
Our Values
Career Opportunities
Join Us
Contact
Blog
Articles
News
Knowledge
e-Books
White Papers
Reports
Webinars
Live Webinars
On-Demand Webinars
Events
Press Release
Success Stories
FAQ
ENG
PL
TR
DE
AR
IT
FR
ES
RO
RU
Book a demo
Book a demo
Home
Blog
News
Germany Clarifies E-Invoice Archiving Rules: GoBD 2025 Amendment: How Businesses Must Now Store EInvoices
Germany Clarifies E-Invoice Archiving Rules: GoBD 2025 Amendment: How Businesses Must Now Store EInvoices
As Germany accelerates toward mandatory B2B e-Invoicing, its Ministry of Finance has […]
August 8, 2025
News
3 min read
As Germany accelerates toward mandatory B2B
e-Invoicing
, its Ministry of Finance has issued a pivotal update to the GoBD, the framework that governs how businesses manage and archive digital financial records. The
second amendment to the GoBD, issued on 14 July 2025
delivers long-awaited clarity on
how e-invoices must be stored
, especially in light of the mandatory e-Invoicing
regime introduced in
January 2025
.
Table of Contents
Toggle
The Structured Format Takes Priority
Visual Representations (PDFs) Are Optional — But Conditional
Original Format Preservation is Mandatory
10-Year Retention with Audit-Ready Access
New Guidance for Hybrid Formats (ZUGFeRD, XRechnung)
No Archiving Needed for Some Payment Service Documents
GoBD 2025: In Effect Now
The Structured Format Takes Priority
The revised GoBD confirms that businesses only need to retain the X
ML file or the structured XML part of a hybrid invoice format like ZUGFeRD
. The
PDF component is not mandatory
, unless it contains additional tax-relevant information.
This is a shift from older interpretations that often assumed visual or printable versions had to be archived alongside XML files. Germany now joins other digital VAT regimes in recognizing structured data as the authoritative source for tax purposes.
Visual Representations (PDFs) Are Optional — But Conditional
For invoices generated through a billing or ERP system,
a PDF version no longer needs to be stored
, provided that:
A human-readable copy can be regenerated at any time
There is no loss of content or discrepancy in interpretation between the structured data and the visual version
However, if a hybrid invoice (like ZUGFeRD) contains tax-relevant details
only in the PDF
, such as posting remarks, payment conditions, or notes, then that PDF must also be archived.
Original Format Preservation is Mandatory
Businesses must
store invoices in the exact format in which they were received
. For example:
If an invoice is received in XML format, that XML must be retained.
If an invoice is sent in a hybrid format, the
structured XML
part is mandatory; the
PDF
part is optional unless it includes additional relevant data.
Format conversions (e.g. XML to PDF) for internal use are permitted, but the
original version must remain intact
and accessible. The GoBD allows enhancements such as OCR (optical character recognition) during scanning, but such additions must be verified and archived.
10-Year Retention with Audit-Ready Access
E-invoices must be archived for
ten years
, and during that period, companies must:
Guarantee
integrity, authenticity, and readability
Retain the invoice in a
machine-readable format
Ensure that tax authorities can
access or request evaluations
of stored data through a read-only interface or structured export
The GoBD update strengthens the concept of
“indirect access” (mittelbarer Datenzugriff)
allowing authorities to demand that businesses analyse and provide structured invoice data themselves or via a third party.
New Guidance for Hybrid Formats (ZUGFeRD, XRechnung)
The update includes detailed clarification around
hybrid invoice formats
, such as:
ZUGFeRD invoices contain both an XML file and a visual PDF
The XML part is considered the
legally binding record
If the PDF includes any
extra tax-relevant info
, it must also be archived
Any
conversion or deletion
of the XML part (e.g. converting to TIFF) is
explicitly prohibited
No Archiving Needed for Some Payment Service Documents
The updated guidance also notes that documents
generated by payment service providers
(e.g. transaction confirmations)
do not need to be stored
, unless:
They serve as
official accounting records
They’re the
only available record
for distinguishing cash and non-cash transactions
This reduces the burden of storing ancillary documents that have no direct accounting function.
GoBD 2025: In Effect Now
These revised requirements are
effective immediately
— from
14 July 2025
. Businesses must review their current archiving practices and technical systems to ensure they are in full alignment.
As Germany continues its multi-year rollout of mandatory e-invoicing for B2B transactions, this GoBD amendment provides critical clarity for IT, finance, and compliance teams. By aligning your systems and policies now, you not only ensure legal conformity but also unlock opportunities for process automation, cost savings, and audit efficiency.
Previous Article
Singapore’s Digital Tax Evolution: Transitioning to the e-Invoicing Era
Next Article
Slovakia Launches Public Consultation on 2027 E-Invoicing and Real-Time Reporting Mandate
Leave a Reply
Cancel reply
Your email address will not be published.
Required fields are marked
*
Comment
*
Name
*
Email
*
Website
Save my name, email, and website in this browser for the next time I comment.
Δ
Archives
January 2026
December 2025
November 2025
October 2025
September 2025
August 2025
July 2025
June 2025
May 2025
April 2025
March 2025
February 2025
January 2025
December 2024
November 2024
October 2024
September 2024
August 2024
July 2024
June 2024
May 2024
April 2024
March 2024
February 2024
January 2024
December 2023
November 2023
October 2023
September 2023
Categories
Articles
e-Book
e-Invoicing
Events
Global Compliance
Knowledge
Live Webinars
News
On-Demand Webinars
Podcasts
Press Release
Reports
SAF-T
Success Stories
Webinars
You may also like
News
Tunisia 2026: e-Invoicing Extends to Services
1. What Has Changed – and What Is Proposed  1.1 Service transactions […]
November 10, 2025
2 min read
News
Latvia’s Leap into Digital Compliance: Mandatory e-Invoicing and Reporting Requirements
Exploring the New Digital Standards for B2G and B2B Transactions  In a […]
November 8, 2024
1 min read
News
Saudi Arabia’s FATOORAH Initiative: Mandatory e-Invoicing from 2025
Starting January 1, 2025, businesses in Saudi Arabia with VATable income over […]
July 14, 2024
1 min read
Company
Our Story
Awards & Certificates
Contact Us
Product
Cloud Platform
Solutions
Partners
Legal
Data Privacy & Security
Cookie Policy
Privacy Policy
Follow Us
Facebook
Twitter
LinkedIn
Youtube
Instagram
© 2026 All Rights Reserved.
Cloud Platform
RTC Suite
Architecture
Benefits & Features
SAP BTP Cockpit
ERP Integration
SAP
Oracle
MS Dynamics
Other ERP
Solutions
Digital Reporting Requirements (DRR)
e-Invoicing
Invoice Reporting
ViDA (VAT in the Digital Age)
e-Waybill
Reporting
SAF-T
VAT Return
CbCR (Country by Country reports)
Intrastat Reports
Plastic Tax Reports
EC Sales List
Automation
AP Automation
e-Banking
Reconciliation
Partners
Partnership Beyond Technology
Referral Partners
Implementation Partners
Technology Alliances
Strategic Partners
Media Partners
Partnership Benefits
Become a Partner
About Us
Company
Our Story
Our Leadership
Data Privacy & Security
Quality & Service
Awards & Certificates
Global Presence
Interoperability Framework
RTC Offices
Career
Our Values
Career Opportunities
Join Us
Contact
Blog
Articles
News
Knowledge
e-Books
White Papers
Reports
Webinars
Live Webinars
On-Demand Webinars
Events
Press Release
Success Stories
FAQ
ENG
PL
TR
DE
AR
IT
FR
ES
RO
RU
Book a demo', '4e458fe5b44d7233d8a7e576d0d39b14d9d409cca5e758b22071e7963e4ac5fe', '{"url": "https://rtcsuite.com/germany-clarifies-e-invoice-archiving-rules-gobd-2025-amendment-how-businesses-must-now-store-einvoices/", "title": "Germany Clarifies E-Invoice Archiving Rules: GoBD 2025 Amendment: How Businesses Must Now Store EInvoices  - RTC Suite", "accessed_at": "2026-01-16T21:18:55.810299+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:18:39.026115+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (38, 'website', 'https://www.dynatos.com/blog/germany-updates-gobd-rules-for-2025-e-invoicing-mandate/', 'Germany updates GoBD rules for 2025 e-invoicing mandate - Dynatos', NULL, 'Germany updates GoBD rules for 2025 e-invoicing mandate
Skip Navigation
Solutions
Themes
E-invoicing mandates
Source-to-Pay maturity
AI in finance and procurement
Process excellence
Compliance and regulations
ESG
Business Spend Management
Cloud
Peppol
XRechnung and ZUGFeRD
KSeF
View all themes
By Business Solutions
Source-to-Pay
Source-to-Pay
AP Automation
AP Automation
AR Automation
AR Automation
Intelligent Document Processing
Intelligent Document Processing
E-invoicing
E-invoicing
SAP process automation
SAP process automation
Order Confirmations
Accounts Payable
Purchase Requisitions
Delivery Notes
Sales Orders
View all business solutions
Case
Efteling
We realized a reduction of
50%
in processing time for delivery notes & invoices.
Read more
Software
Software
Routty
Routty Cloud
Routty AR
Routty AP
Routty IDP
Routty Connectors
Coupa
Microsoft
ISPnext
Tungsten Automation
Tungsten Process Director
Tungsten AP Essentials
Tungsten ReadSoft Invoices
Tungsten e-invoicing network
View all software
Industries
Pharmaceuticals
Banking
Healthcare
Supply Chain
Retail
Manufacturing
Liveblog
Croatia confirms mandatory e-invoicing from 2026
The Croatian Ministry of Finance has published…
Liveblog
France announces e-invoicing pilot phase for early 2026
The French Tax Authority has published detailed…
Resources
Discover
Portfolio
Downloads
Blog
View all resources
Attend
Events
On demand
Webinar
Thu, Jan 29
Routty Partner Winter Update 2026
Read more
Services
Customer services
Support
–
Available 24hours a day.
Implementation
–
Delivering successful projects.
View all services
Supplier onboarding
–
Supplier onboarding as a service.
Advisory
–
Qualitative digital transformation assistance.
Finance Automation
Unleashing the power of AI in Procurement and Finance
The emergence of artificial intelligence (AI) has…
Finance Automation
5 steps to a fully automated invoicing process – Delivery Notes
In the complex world of procurement and…
Company
About us
About Dynatos
Our offices
Partnerships
Become a partner
Frequently Asked Questions
View all about us
Careers
Work at Dynatos
–
Join our talented teams.
Open application
–
We are always looking for talent.
Datasheet
The company overview: solutions & services
Read more
Contact
Close
EN
DE
ES
NL
Skip Navigation
Solutions
Themes
E-invoicing mandates
Source-to-Pay maturity
AI in finance and procurement
Process excellence
Compliance and regulations
ESG
Business Spend Management
Cloud
Peppol
XRechnung and ZUGFeRD
KSeF
View all themes
By Business Solutions
Source-to-Pay
Source-to-Pay
AP Automation
AP Automation
AR Automation
AR Automation
Intelligent Document Processing
Intelligent Document Processing
E-invoicing
E-invoicing
SAP process automation
SAP process automation
Order Confirmations
Accounts Payable
Purchase Requisitions
Delivery Notes
Sales Orders
View all business solutions
Case
Efteling
We realized a reduction of
50%
in processing time for delivery notes & invoices.
Read more
Software
Software
Routty
Routty Cloud
Routty AR
Routty AP
Routty IDP
Routty Connectors
Coupa
Microsoft
ISPnext
Tungsten Automation
Tungsten Process Director
Tungsten AP Essentials
Tungsten ReadSoft Invoices
Tungsten e-invoicing network
View all software
Industries
Pharmaceuticals
Banking
Healthcare
Supply Chain
Retail
Manufacturing
Liveblog
Croatia confirms mandatory e-invoicing from 2026
The Croatian Ministry of Finance has published…
Liveblog
France announces e-invoicing pilot phase for early 2026
The French Tax Authority has published detailed…
Resources
Discover
Portfolio
Downloads
Blog
View all resources
Attend
Events
On demand
Webinar
Thu, Jan 29
Routty Partner Winter Update 2026
Read more
Services
Customer services
Support
–
Available 24hours a day.
Implementation
–
Delivering successful projects.
View all services
Supplier onboarding
–
Supplier onboarding as a service.
Advisory
–
Qualitative digital transformation assistance.
Finance Automation
Unleashing the power of AI in Procurement and Finance
The emergence of artificial intelligence (AI) has…
Finance Automation
5 steps to a fully automated invoicing process – Delivery Notes
In the complex world of procurement and…
Company
About us
About Dynatos
Our offices
Partnerships
Become a partner
Frequently Asked Questions
View all about us
Careers
Work at Dynatos
–
Join our talented teams.
Open application
–
We are always looking for talent.
Datasheet
The company overview: solutions & services
Read more
Contact
Close
Resources
/
Blog
/
Germany updates GoBD rules for 2025 e-invoicing mandate
BMF clarifies digital archiving rules for structured e-invoices
GoBD amendment sets archiving standards for XML-based invoices
The German Ministry of Finance (BMF) has updated the Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form (GoBD). This amendment comes ahead of the mandatory B2B e invoicing start date of January 1, 2025, and aims to align digital archiving requirements with structured e invoicing standards.
One of the key clarifications is that the XML component of a structured e invoice, including hybrid formats such as ZUGFeRD, is the legally relevant element for archiving. If a readable, graphical representation of the invoice can be generated from the XML, companies are no longer required to store a separate PDF version.
The amendment also reinforces that the original file format of a received invoice must be stored. Even if the invoice is converted for internal processing, the original file must remain in the archive to comply with legal retention obligations.
This update provides companies with clearer guidelines on how to handle and store e invoices in the context of GoBD, helping them prepare for the fast approaching e invoicing mandate.
The full text of the amendment is available on the BMF website:
GoBD 2nd amendment (PDF)
Key takeaways
From January 1, 2025, B2B e invoicing becomes mandatory in Germany.
XML is the legally relevant element for archiving structured e invoices.
No separate PDF storage is needed if a visual version can be generated from XML.
The original file format must always be archived, even after conversion.
Companies should review their archiving processes to ensure GoBD compliance.
Additional resources
The official BMF document is relevant for accounting departments, compliance officers, ERP managers, and IT teams responsible for invoice processing and archiving. You can read the full text here:
Download the GoBD amendment (PDF)
Share with your peers
Related documents
Liveblog
March 21, 2025
Spain aligns B2B e-invoicing standards with EU requirements
Spain’s Ministry of Economic Affairs and Digital Transformation has launched…
Read more
Liveblog
June 26, 2024
Estonia blocks ViDA proposal again at June ECOFIN meeting
On June 21, 2024, the ECOFIN meeting ended without an…
Read more
Liveblog
April 30, 2024
New timeline set for Poland’s mandatory KSeF after audit revelations
The Ministry of Finance has just concluded an external audit…
Read more
Want to know more about Dynatos?
Let’s talk
Our software
Tungsten Automation
ISPnext
Coupa
Microsoft
Routty
Our resources
Portfolio
Downloads
Events
On demand
Blog
Our company
About us
Become a partner
Support
FAQ
See all
What can we do for you?
Contact us
©2026 – Dynatos. All Rights Reserved.
Privacy policy
Cookie policy
Menu', 'ff9541882c0c6b5612759e79430c0c7aad91d28c5f5fbf9a1f936c6aaeb7d005', '{"url": "https://www.dynatos.com/blog/germany-updates-gobd-rules-for-2025-e-invoicing-mandate/", "title": "Germany updates GoBD rules for 2025 e-invoicing mandate", "accessed_at": "2026-01-16T21:18:57.460299+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:18:57.461033+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (39, 'website', 'https://www.taxathand.com/article/40479/Germany/2025/MOF-publishes-further-administrative-guidance-on-mandatory-domestic-B2B-e-invoicing', 'E-invoicing - Deloitte | tax@hand', NULL, 'Access Denied
Sorry, access is denied!
Weâve identified an issue and prevented your access.
The details: Deloitte WAF Solution
Client IP:
2a02:908:c20c:4f00:dabb:c1ff:fe96:f489
Reference ID:
18.451dc917.1768598337.82791e55
Need help? No problem. Get in touch with Global Service Desk and provide the
Reference ID
, and weâll see how we can help.
Global ServiceNow Portal:
Report an Issue
Assignment Group:
DTTL-Cybersecurity-WebApplicationFirewall
Global WAF ServiceNow form:
Web Application Firewall (WAF)', '2a3f69b6762789531fe65113b0f892ac141203481a4f3ffa0068481bfac6f806', '{"url": "https://www.taxathand.com/article/40479/Germany/2025/MOF-publishes-further-administrative-guidance-on-mandatory-domestic-B2B-e-invoicing", "title": "Access Denied", "accessed_at": "2026-01-16T21:18:57.783604+00:00", "status_code": 200, "content_type": "text/html"}', '2026-01-16T21:18:57.785634+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (40, 'website', 'https://eclear.com/article/mandatory-e-invoicing-from-2025-vat-pitfalls-and-practical-recommendations/', 'Mandatory E-Invoicing in Germany 2025: VAT Risks & Tips - eClear', NULL, 'Mandatory E-Invoicing in Germany 2025: VAT Risks & Tips
Products
For marketplaces and platforms
ClearVAT
Cross-border E-Commerce VAT-free
ClearCustoms
Border-free commerce
Corporate Tax
CustomsAI
Customs Tariff Classification
VATRules
Database for VAT Rates and Rules
FileVAT
VAT declaration for EU-27, CH, NO, UK
CheckVAT ID
Audit-proof VAT ID Check
Solutions
Integrations
CheckVAT ID Online-Tool
SAP
Newsroom
News
Latest Information
Events
Newsletter
eClear in the media
Special topics
Cross-border E-Commerce
Customs - Infos & News
VAT - Infos & News
Knowledge:
ViDA for marketplaces and platforms
EU Directive on Administrative Cooperation (DAC)
Import-One-Stop-Shop (IOSS)
One-Stop-Shop (OSS)
VAT Rates in Europe
EU-Asia Trade Simplified: Your Customs and Compliance Guide
Tech:
Tax Technology
E-Commerce Tools
About us
Mission Europe
Management Board
Supervisory Board
Partners
Locations
Career
Contact
EN
DE
Close
Products
Products
For marketplaces and platforms
ClearVAT
Cross-border E-Commerce VAT-free
ClearCustoms
Border-free commerce
Corporate Tax
CustomsAI
Customs Tariff Classification
VATRules
Database for VAT Rates and Rules
FileVAT
VAT declaration for EU-27, CH, NO, UK
CheckVAT ID
Audit-proof VAT ID Check
Solutions
Solutions
Integrations
CheckVAT ID Online-Tool
SAP
Newsroom
Newsroom
News
Latest Information
Events
Newsletter
eClear in the media
Special topics
Cross-border E-Commerce
Customs - Infos & News
VAT - Infos & News
Knowledge:
ViDA for marketplaces and platforms
EU Directive on Administrative Cooperation (DAC)
Import-One-Stop-Shop (IOSS)
One-Stop-Shop (OSS)
VAT Rates in Europe
EU-Asia Trade Simplified: Your Customs and Compliance Guide
Tech:
Tax Technology
E-Commerce Tools
About us
About us
Mission Europe
Management Board
Supervisory Board
Partners
Locations
Career
[wpdreams_ajaxsearchlite]
Contact
EN
DE
Home
·
Newsroom
·
E-Invoicing
·
Mandatory E-Invoicing from 2025 – VAT Pitfalls and Practical Recommendations
E-Invoicing
,
Newsroom
|  12. January 2026
Mandatory E-Invoicing from 2025 – VAT Pitfalls and Practical Recommendations
Mandatory e-invoicing from 2025 is far more than a technical requirement. It directly affects key VAT principles, particularly the right to deduct input VAT. Companies that address the transition early from a VAT perspective can minimize risks while benefiting from more efficient processes.
by
eClear
1. Background: What Will Change from 2025?
With the German Growth Opportunities Act (Wachstumschancengesetz), the mandatory use of e-invoices in domestic B2B transactions will be introduced in stages. From 1 January 2025, businesses must be able to receive e-invoices; from 2027 or 2028 (depending on annual turnover), they will also be required to issue e-invoices.
From a VAT perspective, it is important to note that the legal definition of an invoice remains unchanged and continues to be based on Section 14 of the German VAT Act (UStG). However, an invoice will only qualify as an e-invoice if it is issued in a structured electronic format (e.g. XRechnung or ZUGFeRD version 2.0.1 or higher) that allows for automated processing.
2. Distinction: E-Invoice vs. Other Electronic Invoices
A common misconception in practice is to treat PDF invoices as e-invoices. For VAT purposes, the distinction is clear:
E-invoice: structured electronic format
Other electronic invoice: PDF, scanned document, email attachment
From 2025 onwards, PDF invoices will generally no longer be sufficient for domestic B2B transactions, unless a transitional rule applies. Companies must therefore ensure that they are able to clearly distinguish between different invoice types from both a technical and organizational perspective.
3. Input VAT Deduction: Where Are the Risks?
One of the most significant VAT risk areas concerns the right to deduct input VAT. As before, this right requires a proper invoice. If an e-invoice does not comply with formal requirements or uses an invalid format, the input VAT deduction may be denied during a tax audit.
Particularly critical issues include:
missing or incorrect mandatory invoice details (Section 14(4) UStG)
non-compliant data formats
media discontinuities between e-invoicing and accounting systems
missing linkage between the invoice and the underlying supply or service
Increased automation also means that errors may occur systematically and on a large scale, amplifying potential risks.
4. Impact on Internal Processes
The introduction of e-invoicing is not merely an IT project. VAT-relevant processes affected include:
invoice receipt and verification
approval and posting workflows
archiving in compliance with GoBD requirements
interfaces between ERP systems, accounting, and tax modules
Companies should review whether VAT checks are carried out before or only after posting, as this can be decisive in minimizing risks.
5. VAT-Focused Recommendations for Action
To reduce VAT risks, companies should take early and structured action:
Analyze invoice flows (incoming and outgoing invoices)
Verify invoice formats for VAT compliance
Adapt input VAT controls to automated processes
Train relevant departments (not IT alone)
Ensure close coordination between tax, accounting, and IT teams
6. Conclusion
Mandatory e-invoicing from 2025 is far more than a technical requirement. It directly affects key VAT principles, particularly the right to deduct input VAT. Companies that address the transition early from a VAT perspective can minimize risks while benefiting from more efficient processes.
Author
eClear
More articles by eClear
Write email
Share on LinkedIn
Share on Twitter
Share on Facebook
Links
EU-wide VAT Gap Report 2023 – VAT Revenue Losses and Solutions
Mandatory VAT Reporting in Digital Commerce – New Transparency Requirements in Focus
New EU VAT Rules for Imports Starting 2028 – What Businesses and Consumers Need to Know
More on the subject:
E-Invoicing
E-Invoicing
| 22. September 2023
E-Invoicing in the EU: The Quick Business Guide
In the dynamic world of e-commerce, the ability to adapt and evolve is not just a competitive advantage but a…
Customs
,
E-Commerce
,
Market insights
,
Newsroom
,
VAT
| 19. September 2023
Malta Clarifies DAC 7 for Platforms
This week''s Commerce Updates brings you critical insights: From Malta''s latest clarification on DAC 7 guidelines affecting platform operators to…
Customs
,
E-Commerce
,
Market insights
,
Newsroom
,
Payment
,
VAT
| 1. August 2023
Spain’s EU Presidency: A New Era for Taxation
Welcome to this edition of the Commerce Updates, where we bring you the latest developments shaping the world of trade…
You might also be interested in:
Newsroom
,
VAT
| 19. December 2025
EU-wide VAT Gap Report 2023 – VAT Revenue Losses and Solutions
The EU-wide VAT Gap Report 2023 published by the European Commission shows that the gap between theoretically due VAT and…
E-Commerce
,
Newsroom
,
VAT
| 4. December 2025
Mandatory VAT Reporting in Digital Commerce – New Transparency Requirements in Focus
The digitalization of commerce is prompting tax authorities across Europe to introduce new instruments to combat VAT fraud and increase…
Newsroom
,
VAT
| 28. November 2025
New EU VAT Rules for Imports Starting 2028 – What Businesses and Consumers Need to Know
From July 1, 2028, sellers and online marketplaces – including those outside the EU – will be required to collect…
Products
ClearVAT
ClearCustoms
CustomsAI
VATRules
FileVAT
CheckVAT ID
Company
About us
Management Board
Supervisory Board
Partners
Locations
Career
Newsroom
Current information
Newsletter
Glossary
Contact
eClear AG
Französische Straße 56-60
10117 Berlin, Germany
info@eclear.com
WKN: A2AA3A
ISIN: DE000A2AA3A5
Contact
Customer support
Social
©2026 eClear Aktiengesellschaft
Privacy policy
Imprint
Contact', '75f317646d65f2e1381afb75c8c98fc59edb9ced1a5e656a3237e463f98fb04e', '{"url": "https://eclear.com/article/mandatory-e-invoicing-from-2025-vat-pitfalls-and-practical-recommendations/", "title": "Mandatory E-Invoicing in Germany 2025: VAT Risks & Tips", "accessed_at": "2026-01-16T21:18:58.726791+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:18:58.727611+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (41, 'website', 'https://www.avalara.com/blog/en/europe/2024/03/germany-mandatory-e-invoicing-2025.html', 'Germany has implemented mandatory e-invoicing January 2025', NULL, 'Germany has implemented mandatory e-invoicing January 2025
Skip to main content
Agentic AI
Solutions
Browse by tax type
VAT
Streamline VAT determination, e-invoicing, and reporting
Sales tax
Retail, ecommerce, manufacturing, software
Import One-Stop Shop (IOSS)
Simplify VAT registration requirements
Selling Internationally
Customs duties and import taxes
Industries
Retail
For online, ecommerce and bricks-and-mortar shops
Software
For apps, downloadable content, SaaS and streaming services
Manufacturing
For manufacturers with international supply chains
Browse by Business Type
Accounting professionals
Partnerships, automated solutions, tax research, education, and more
Business process outsourcers
Better serve the needs of clients
Marketplace sellers
Online retailers and ecommerce sellers
Shared service centres
Insource VAT compliance for your SSC
Business Size
Enterprise
An omnichannel, international sales solution for tax, finance, and operations teams
Products
Overview
Our platform
Product categories
Calculations
Calculate rates with AvaTax
Returns & Reporting
Prepare, file and remit
VAT Registrations
Manage registrations, simply and securely
VAT Solutions
Streamline VAT determination, e-invoicing, and reporting
Fiscal
Fiscal representation
Content, Data, and Insights
Research, classify, update
Exemption Certificate Management
Collect, store and manage documents
Featured Products
Avalara E-Invoicing and Live Reporting
Compliant in over 60 countries
Making Tax Digital (MTD)
Comply with MTD Phase 2
See all products
Resources
Learn and connect
Blog
Tax insights and updates for Europe
Webinars
Free advice from indirect tax experts
Events
Join us virtually or in person at Avalara events and conferences hosted by industry leaders
Whitepapers
Expert guidance and insights
Featured Resources
Reverse Charge VAT
VAT and customs guidance
Digitalisation of tax reporting
Realtime VAT compliance (including MTD)
Selling into the USA
Sales tax for non-US sellers
Know your nexus
Sales tax laws by U.S. state
Free tools
EU Rates
At-a-glance rates for EU member-states
Global Rates
At-a-glance rates across countries
U.S. Sales Tax Risk Assessment
Check U.S. nexus and tax responsibilities
EU VAT Rules
EU VAT Registration
EU VAT Returns
Distance Selling
EU VAT digital service MOSS
Resource center
Partners
Existing Partners
Partner Portal
Log in to submit referrals, view financial statements, and marketing resources
Submit an opportunity
Earn incentives when you submit a qualified opportunity
Partner Programs
Become a partner
Accountant, consulting, and technology partners
Become a Certified Implementer
Support, online training, and continuing education
Find a partner
Avalara Certified Implementers
Recommended Avalara implementation partners
Developers
Preferred Avalara integration developers
Accountants
State and local tax experts across the U.S.
Integrations
Connect to ERPs, ecommerce platforms, and other business systems
About
About
About Avalara
Customer stories
Locations
Jobs
Get started
Get started
Sales
phone_number
Sign in
Blog
Blog
Location
North America
India
Europe
Tax type
Sales tax
Use tax
VAT
GST
Duties and tariffs
Property tax
Excise tax
Occupancy tax
Communications tax
Need
AI
Tax calculation
Tax returns
E-invoicing
Exemption certificates
Business licenses
Registrations
1099 and W-9
Cross-border
Tax changes
Sales tax holidays
IOSS and OSS
Sales tax nexus
Digital goods and services
Shipping
Online selling
Product taxability
Industry
Manufacturing
Retail
Software
Hospitality
Short-term rentals
Accounting
Communications
Government
Supply chain and logistics
Energy
Tobacco
Beverage alcohol
Business and professional services
Restaurants
Marketplace facilitators
Related
Resource center
Webinars
Share:
Share to Facebook
Share to Twitter
Share to LinkedIn
Copy URL to clipboard
German e-invoicing mandate updates
Kamila Ferhat
May 12, 2025
Last updated on May 12, 2025.
Get the latest updates on e-invoicing mandates and live reporting requirements in Germany. Businesses operating in Germany should stay informed about these developments and take necessary steps to help ensure they can operate compliantly as the e-invoicing landscape evolves.
Germany e-invoicing mandate timeline
May 2025: Germany updates national e-invoicing format
Electronic Invoice Forum Germany (FeRD) announced on May 7, 2025, an update to ZUGFeRD to coincide with France’s Factur-X1.07 update. ZUGFeRD 2.3 includes updates to Code Lists to align with requirements set out in EN16931, plus editorial and schematron corrections.
January 2025: B2B e-invoicing begins in Germany
The first phase of Germany’s business-to-business (B2B) e-invoicing mandate comes into effect, requiring German businesses to be able to receive e-invoices for B2B transactions. This requirement applies to all businesses, regardless of size or annual turnover. Acceptable e-invoicing formats must be compatible with standards outlined in European Norm (EN) 16931, such as XRechnung and ZUGFeRD. There are a small number of exemptions, including invoices under €250 and tickets for passenger transport. As part of Germany’s phased approach to implementing e-invoicing, the requirement to issue e-invoices for businesses of all sizes and turnover will be in place by January, 2028.
October 2024: Germany offers further e-invoicing guidance for businesses
The Federal Ministry of Finance (BMF) publishes the final version of guidance
for implementing Germany’s B2B e-invoicing mandate, being introduced from January 1, 2025. The guidance includes details on e-invoicing requirements and accepted formats, such as XRechnung and ZUGFeRD.
August 2024: Germany details ‘soft approach’ to e-invoicing
Germany will take a soft approach to implementing e-invoicing
, with transition periods and flexibility for smaller businesses in particular. German authorities clarify that while all businesses must be capable of receiving e-invoices by January 2025, issuing them remains optional until 2027. Germany also clarifies that from January 2025, an e-invoice will be defined as “an invoice that is issued, transmitted and received in a structured electronic format and enables electronic processing” — such as XRechnung. Standard PDFs created, transmitted, and received electronically will not be considered e-invoices.
March 2024: Germany announces mandatory e-invoicing from January 2025
The Growth Opportunities Act introduces various tax measures, including a mandate for B2B e-invoicing.
Germany outlines plans to implement e-invoicing in phases
, starting with a mandate to receive structured e-invoices from January, 2025, followed by a broader requirement to issue structured e-invoices from January 2028.
November 2022: Germany seeks EU e-invoicing mandate approval
The German government formally
asks the European Commission for permission to mandate e-invoicing
, beginning with B2B transactions. The government believes that e-invoicing will “...significantly reduce the susceptibility to fraud of our VAT system and modernise and at the same time reduce the bureaucracy of the interface between the administration and the businesses.”
Ready for e-invoicing?
Avalara E-Invoicing and Live Reporting
can help you comply with global mandates and reporting requirements as they evolve.
Share:
Share to Facebook
Share to Twitter
Share to LinkedIn
Copy URL to clipboard
Cross-border
E-invoicing
Germany
Tax and compliance
Sales tax rates, rules, and regulations change frequently. Although we hope you''ll find this information helpful, this blog is for informational purposes only and does not provide legal or tax advice.
Kamila Ferhat
Avalara Author
Recent posts
Dec 19, 2025
Preparing for Making Tax Digital in 2026: What U.K. businesses should know
Dec 16, 2025
Unlocking global e-invoicing: How enterprises can scale strategically
Nov 25, 2025
The end of the €150 customs duty exemption for low-value imports into the EU: What businesses need to know
Avalara Tax Changes 2026 is here
The 10th edition of our annual report engagingly breaks down key policies related to sales tax, tariffs, and VAT.
Read the report
Stay up to date
Sign up for our free newsletter and stay up to date with the latest tax news.
About Avalara
About Avalara
Careers
Customer insights
Partner program
Support
Products & Services
Avalara VAT Reporting
VAT Registration & Returns
AvaTax Calculation software
MTD Cloud
Avalara E-Invoicing and Live Reporting
Resources
VATLive blog
Webinars
Whitepapers
Get EU VAT number
Help with VAT returns
Contact Us
+44 (0) 1273 022400
Monday – Friday
8:00am – 6:00pm
Monday - Friday
8:00 a.m.-6:00 p.m. GMT
Europe (English)
Europe (English)
Australia (English)
Brazil (English)
Brasil (Português)
France (Français)
Germany (Deutsch)
India (English)
New Zealand (English)
Singapore (English)
United States (English)
Terms
Cookies
Privacy
Anti-Slavery Disclosure
© Avalara, Inc. {date}', '520159e85594d37b79b4881df85eb37783b154204c70f84bdc0a56587e6cd9f1', '{"url": "https://www.avalara.com/blog/en/europe/2024/03/germany-mandatory-e-invoicing-2025.html", "title": "Germany has implemented mandatory e-invoicing January 2025", "accessed_at": "2026-01-16T21:18:58.901098+00:00", "status_code": 200, "content_type": "text/html;charset=utf-8"}', '2026-01-16T21:18:58.901873+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (42, 'website', 'https://easy-software.com/en/glossary/gobd-principles-for-proper-bookkeeping/', 'GoBD – Principles for proper bookkeeping - Easy Software', NULL, 'The GoBD - What is it? Definition & explanation
Skip to content
easy portal
contact
×
language
Global (English)
Deutschland | Schweiz
Contact
Menu
Menu
Solutions
Application Areas
Know-how
Service & Support
Partner
About easy
language
Global (English)
Deutschland | Schweiz
Contact
×
Solutions
Powerful
ECM
solutions
Digital archiving, accounts payable, contracting, and HR management systems are available as on-premises, private cloud, cloud-native, or hybrid solutions. Intelligent workflows and AI services automate document-based processes.
easy
archive
Compliant archiving
easy
invoice
Secure invoice management
easy
DMS
Efficient document management
easy
contract
Transparent contract management
easy
hr
Smart HR management
SAP
Add-ons
Experience digitization easily and quickly with third-party systems, such as SAP
®
. easy provides SAP-certified add-ons.
learn more
Application Areas
references
Banks and insurances
Energy industry
Manufacturing and production
Public service and associations
View all industries
application areas
Archiving
Document Capture
Procurement Processes
Digital Personnel File
Digital Signature
Document Management
Incoming Invoices
Electronic File
Contract Management
Workflows
successful customers
Read success stories from some of the 5400+ easy customers who are digitally transforming their businesses with easy products.
case studies
Know-how
Knowledge
Browse the blog. Use free white papers, checklists and guidelines and expand your knowledge in webinars and at events.
Newsroom
Blog
Press & News
Glossary
Events & Whitepaper
Events & Live Webinars
Knowledge base
Guides & Checklists
Webinar Recordings
Whitepaper
Digital accounts payable processing
Find out in our white paper how your company can benefit from electronic incoming invoice processing.
free whitepaper
Service & Support
Resources
Develop your potential with the easy Academy, optimize your business with Managed Services and get help from professional easy Support.
Service
easy Academy
Managed Services
Support
Support
As a direct customer, you can benefit from our support.
support
Partner
Partner Network
Benefit from our experience as a software manufacturer for ECM, SAP, Cloud and SaaS solutions. We want to grow together with our software partners.
Partners
Become a partner
Find partners
easy Portal
Become a partner
Become part of our partner community and expand your business model with the successful easy product portfolio.
become a partner
About easy
easy group
With over 30 years of experience, easy software is one of the market leaders for archiving, ECM, and DMS software solutions in the German-speaking region.
About Easy
Company
Press & News
Customer Stories
Contact Us
If you have any questions, just let us know.
contact us
Solutions
easy
archive
Compliant archiving
easy
invoice
Secure invoice management
easy
contract
Transparent contract management
easy
hr
Smart HR management
easy
DMS
Efficient document management
SAP
Add-ons
Experience digitization easily and quickly with third-party systems, such as SAP
®
. easy provides SAP-certified add-ons.
learn more
Application Areas
references
Banks and insurances
Energy industry
Manufacturing and production
Public service and associations
View all industries
application areas
Archiving
Document Capture
Procurement Processes
Digital Personnel File
Digital Signature
Document Management
Incoming Invoices
Electronic File
Contract Management
Workflows
successful customers
Read success stories from some of the 5400+ easy customers who are digitally transforming their businesses with easy products.
case studies
know-how
Knowledge
Browse the blog. Use free white papers, checklists and guidelines and expand your knowledge in webinars and at events.
Newsroom
Blog
Press & News
Glossary
Events & Whitepaper
Events & Live Webinars
Knowledge base
Guides & Checklists
Webinar Recordings
Whitepaper
Digital accounts payable processing
Find out in our white paper how your company can benefit from electronic incoming invoice processing.
free whitepaper
Service & Support
Resources
Develop your potential with the easy Academy, optimize your business with Managed Services and get help from professional easy Support.
Service
easy Academy
Managed Services
Support
Support
As a direct customer, you can benefit from our support.
support
Partner
Partner Network
Benefit from our experience as a software manufacturer for ECM, SAP, Cloud and SaaS solutions. We want to grow together with our software partners.
Partners
Become a partner
Find partners
easy Portal
Become a partner
Become part of our partner community and expand your business model with the successful easy product portfolio.
become a partner
About easy
easy group
With over 30 years of experience, easy software is one of the market leaders for archiving, ECM, and DMS software solutions in the German-speaking region.
About Easy
Company
Press & News
Customer Stories
Contact Us
If you have any questions, just let us know.
contact us
easy portal
Glossary
GoBD
– Principles for proper bookkeeping
The principles for the proper keeping and storage of books, records and documents in electronic form and for data access are binding guidelines issued by the Federal Ministry of Finance (BMF) to tax authorities for digital bookkeeping and archiving.
As an
administrative regulation
, it governs how electronic business data must be recorded, processed, stored and retained in order to meet tax law requirements. The administrative regulation is primarily aimed at tax offices at federal, state or local level.
Requirements
for companies
arise
directly
from this regulation
.
Importance of the
GoBD
for companies
The GoBD ensure that tax-relevant data is archived in a tamper-proof and traceable manner. This includes in particular
Immutability
: Once recorded, data may not be changed without documentation.
Traceability
and
verifiability
: All postings must be comprehensible and fully traceable for audit authorities.
Regularity
: Data must be processed systematically and correctly.
Retention obligation
: Digital receipts and documents must be archived for up to ten years, depending on their type.
Data access
: The
tax authorities
have the right to direct or indirect access to tax-relevant data.
GoBD
and ECM systems
Enterprise content management
(ECM) systems support companies in efficiently implementing the GoBD requirements. Modern ECM solutions offer
Audit-proof archiving
of documents and receipts
Automated logging
of all changes and accesses
Access controls
to ensure data integrity
Digital workflows
for compliance with GoBD requirements
In many cases, an ECM system forms the basis for many other applications. It often includes
document management
to map document-intensive business processes, such as incoming invoice processes.
Conclusion
Compliance with the GoBD is essential for companies to ensure tax security and make digital processes legally compliant. An ECM system helps to meet the requirements efficiently and ensures secure, audit-compliant document management.
FAQ on
GoBD
Which documents are subject to the
GoBD
?
All tax-relevant documents fall under the principles. According to Section 147 (1) of the German Fiscal Code (AO), this includes books, records, management reports, annual financial statements, inventories, business and commercial letters and all types of
accounting vouchers
.
Are PDF documents subject to the
GoBD
?
Yes
, as soon as the PDFs are fiscally relevant. Incidentally, e-invoicing will be mandatory from January 2025 in Germany. As a result, invoices must be transmitted as a
ZUGFeRD PDF
or as an XRechnung and archived as an e-invoice in compliance with GoBD.
What are the consequences of
GoBD
violations?
As long as the bookkeeping is traceable and verifiable, violations of the GoBD do not necessarily lead to negative results.
If this is not the case
, there may be significant
additional tax estimates
.
To whom does the
GoBD
apply?
Die Grundsätze gelten für alle Unternehmen und Selbstständigen in Deutschland, die steuerlich relevante Daten verarbeiten. Dies umfasst insbesondere:
Companies
: All types of companies, regardless of their size or sector, must comply with the GoBD.
Self-employed persons
: Sole traders and freelancers are also obliged to comply with the principles.
Organizations
: Non-profit organizations and associations that process tax-relevant data must also comply with these principles.
easy
archive
Archive data securely and compliant.
discover easy archive
easy
invoice
Digitally verify and approve invoices.
discover easy invoice
Newsroom
Media Library
Glossary
Contact us
+1 267 313 57-80
info[at]easy-software.com
Newsletter
We will keep you regularly up to date. Subscribe to our newsletter and find out everything you need to know about the digitization of business processes. The topics will be prepared for you in a tailor-made and varied way.
Newsletter subscription
Solutions
easy
archive
easy
invoice
easy
contract
easy
hr
easy
DMS
Application areas
Archiving
Document Capture
Procurement Processes
Digital Personnel File
Digital Signature
Document Management
Incoming Invoices
Electronic File
Contract Management
Workflows
Resources
Support
Academy
Managed Services
Blog
Media Resources
Events
Customer Stories
Glossary
About easy
Company
Press & News
Investor Relations
Find partners
Application areas
Archiving
Document Capture
Procurement Processes
Digital Personnel File
Digital Signature
Document Management
Incoming invoices
Electronic File
Contract Management
Workflows
Resources
Support
Academy
Managed Services
Blog
Media Resources
Events
Customer Stories
Glossary
About easy
Company
Press & News
Investor Relations
Careers
Find partners
Social
easy
is a
conrizon
brand offering established software products for compliant archiving, invoice processing, contract management, and human resources management. The right solution for every challenge, industry and company size.
www.conrizon.com
Imprint
General terms and conditions
Disclaimer
Privacy
Privacy Settings
Search for:', '4502e6c53e7d65f39babe8c76e6c06acb41dc529abbd32c0c3769d3c216ab90a', '{"url": "https://easy-software.com/en/glossary/gobd-principles-for-proper-bookkeeping/", "title": "The GoBD - What is it? Definition & explanation", "accessed_at": "2026-01-16T21:23:10.551044+00:00", "status_code": 200, "content_type": "text/html; charset=UTF-8"}', '2026-01-16T21:19:16.498704+00:00');
INSERT INTO sources ("id", "type", "identifier", "name", "version", "content", "content_hash", "metadata", "created_at") VALUES (43, 'website', 'https://www.getpliant.com/en-us/blog/what-is-gobd/', 'Is your business GoBD compliant?', NULL, 'Is your business GoBD compliant?
home page
Products
OPTIMIZE CARD PAYMENTS
Payment Apps
Pro API
BUILD YOUR OWN PROGRAM
Cards-as-a-Service
CardOS
Discover Payment Apps
→
Conveniently manage all your company''s card payments
Features
Real time monitoring
Receipt management
Spend control
Accounting automations
Multi-currency accounts
Benefits
Integrations
Discover Pliant Pro API
→
Automate your payment processes via API
Features
Card issuance & management
Transaction insights
Accounting optimization
Member management
Custom integrations
Discover Cards-as-a-Service (CaaS)
→
Build your own custom credit card offering
Features
Card issuance & management
Advanced data capabilities
Ready-made UI
Compliance & security
Dedicated support
CaaS API
Integrations
Discover CardOS
→
Launch best-in-class credit card programs for banks
Features
Accounting automation & integrations
Next-generation financial infrastructure
Modular architecture & detailed customization
Scalable back-office tools
Flexible integration
Cards
Cards
See the advantages of all our different credit cards
Use Case
Payment Technology
Travel Purchasing Cards
Lodge Cards
Fleet Cards
Employee Benefit Cards
Insurance Claim Cards
Emergency Disbursement Card
Physical Cards
Premium Cards
Virtual Cards
Single-use Cards
Solutions
Solutions
How customers from key industries benefit from Pliant
OPTIMIZE CARD PAYMENTS
Corporations
E-commerce
Marketing agencies
Resellers
SaaS
Travel industry
BUILD YOUR OWN PROGRAM
ERP
Invoice management
Travel expense management
Specialised lending
Banking
Insurance payments
Recent customer stories
All customer stories
→
Salabam Solutions
“Pliant Pro API is a key asset to our travel booking platforms.”
Travel
Circula
"Circula will process €100 million in card spend this year"
Travel expense management
acocon
"Thanks to Pliant, we''ve been able to win customers from other Atlassian partners.”
Resellers
Resources
Resources
All the detailed information about Pliant for both visitors and customers
Pricing
Exchange rates
Help center
Blog
Events
FAQ
Press
Careers
Contact
Revenue calculator
Recent blog posts
All blog posts
→
Building Real-Time, Reliable Notifications with Server-Side Events: A Case Study from Fintech
Virtual Credit Cards for Employees: What You Need to Know
TMC-tailored virtual credit cards: Cashback at your fingertips
What is a Card Issuance Provider? And who would benefit from issuing their own credit cards?
Developers
Developers
Build your all-in-one credit card solution with Pliant
Pro API
Documentation
Changelog
API Reference
Status
CaaS API
Documentation
Changelog
API Reference
Status
Developers Starter Guide
Sales
:
+1 (917) 540 4658
Login
Get started
Business
5 min read
Is your business GoBD compliant?
Whether you''re interested in starting a business or expanding your operations to Germany, making sure your company is GoBD compliant should be one of your top priorities.

In this quick read, we’ll address the most relevant aspects you should consider before taking the big leap into the German market.
Duline Theogene
on
12/6/2022
Table of content
What is GoBD?
Who regulates the GoBD?
What are the GoBD principles?
What happens if you do not comply with GoBD?
Ways to present data to auditors according to the GoBD
Final Thoughts
💡
Quick note:
For more detailed information and legal and financial advice, contact your tax advisor.
What is GoBD?
Conveniently, GoBD is short for
Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff.
Which in English translates to “
Basic Principles on The Proper Keeping and Storage of Financial Books, Recordings, and Documents in Electronic Form as Well as Data Access.”
Who regulates the GoBD?
The German Federal Ministry of Finance has been regulating these principles since 2014. And in January 2020, an updated version came into effect.
What are the GoBD principles?
As mentioned previously, in Germany, the principles of proper accounting are known as ''GoB''; the added ''D'' refers to ''digital data'' or ‘electronic form.’
They''re based on the
German Fiscal Code (AO)
, and they make the digitization and management of financial documentation possible.
Their main purpose is to ensure that both entrepreneurs and companies maintain and process their physical and digital
financial records
, also known as ''books''
, in
tip top shape.
The GoBD principles can be listed and summarized as follows:
1. Traceability and Verifiability
As the name implies, your financial records must be easy to track and verify. In order to do so, you should have a
reliable invoice management system
.
We also recommend that you prepare a documentation process describing how your tax information is or will be handled, including:
Internal Control System (ICS)
all types of company expenses and revenue streams
relevant financial processes
the way in which receipts and invoices are handled,
etc.
This will be extremely useful if you plan to hire a third party to manage your finances as it will give them a clear picture of the financial state of your company and the procedures you have in place to fulfill
your tax obligations.
2. Principles of Truth
Your tax auditor must be able to ensure that all your transactions are governed by honest practices and therefore processed in reliable systems, always abiding by the GoBD standards.
3. Clarity and Continuous Recording
Keep in mind that all your physical and digital financial records must remain available from 6 to 10 years in case you''re required to submit them for review.
4. Completeness
Each document subject to review must be recorded in full.
Invoices, for example, must contain at least the following data:
Date of issuance
Invoice sequential number
VAT business information
Business and customer address
Legal entity (GmbH, AG, KG, OHG, e.K)
Tax number
Complete and detailed description of sale or service provided
VAT amount or VAT exception if applicable (i,e. zero VAT rate or reverse charge)
5. Individual Recording Obligation
Even if you''re planning to hire a third party company or software, to handle your bookkeeping, bear in mind that only you--as the owner of the company, will be responsible for being compliant at the time of the audit as stipulated by article 33 of the AO (German Fiscal Code).
6. Correctness
As you can imagine, your tax-relevant data must be double-checked before submitting it for audit review to avoid any mishaps.
7. Timely Bookings and Records
It is critical that your accountant or finance teams educate your employees around this important issue.
Your staff should scan and submit invoices immediately after an expense is made
so that at the end of the month all receipts are recorded and available in your
accounting system
with the expenses made in that period.
8. Order and Immutability
Your records should be kept in an orderly and chronological fashion. This way, a tax auditor will be able to access them quickly.
Immutability means that no receipt or invoice should be altered or overwritten.
What happens if you do not comply with GoBD?
In the event that your company does not comply with the GoBD principles, your documents will be subject to a second audit or you could find yourself in a
back tax issue.
This is all likely caused by:
not reporting correct taxes the previous year,
not filing a tax return,
reporting incorrect income, or
missing a deadline.
Ways to present data to auditors according to the GoBD
According to the latest update of these principles, companies or entrepreneurs can present digital tax-relevant data  in three ways:
1. Direct access (Z1)
2. Indirect access (Z2)
3. Data medium provision (Z3)
Direct access (Z1)
allows the auditor to have immediate digital access to the company''s records or systems to be audited.
Since the auditor won''t be responsible for any type of modification or error, we recommend that you give them ''read-only'' permission to your records. This way, they won''t be able to edit or modify any data by mistake.
With
indirect access (Z2)
, the auditor must be physically present at the company. There, you will be give them digital access to all necessary documents.
Data medium provision (Z3)
grants the information to be audited on a digital medium stipulated by the auditors themselves. This can be in a type of data analysis software normally used by auditors (i.e,
IDEA
).
Final Thoughts
Tax and data compliance is an issue of great concern for every entrepreneur exploring a new business venture. More specifically if we’re referring to business owners looking to expand into the German market.
We hope we have answered all your initial questions about the GoBD principles
, and we remind you to contact your tax advisor if you want to know more detailed information tailored to your business'' needs.
If you need a smart expense management solution for your company, we suggest you
book a demo
with our team.
Duline Theogene
LinkedIn icon
Content Marketing Manager
Table of content
What is GoBD?
Who regulates the GoBD?
What are the GoBD principles?
What happens if you do not comply with GoBD?
Ways to present data to auditors according to the GoBD
Final Thoughts
Are you ready for modern credit cards?
Talk to a Pliant Expert Today!
Thank you for your interest.
We will send you more information.
Something went wrong!
Please try again.
Recent blog posts
All blog posts
Building Real-Time, Reliable Notifications with Server-Side Events: A Case Study from Fintech
In this post, we’ll explore how we designed and implemented a scalable, event-driven architecture based on Server-Side Events (SSE) to handle these kinds of user-facing workflows in real time.
Tech at Pliant
4 min read
Virtual Credit Cards for Employees: What You Need to Know
A modern virtual card solution for employees is secure, transparent, and saves time for management, employees, and accountants through streamlined digital processes.
Credit cards
11 min read
TMC-tailored virtual credit cards: Cashback at your fingertips
Travel Management Companies (TMCs) make business travel easy for their clients. A powerful modern corporate credit card solution ensures that TMCs’ internal processes and operations run equally smoothly.
Travel
5 min read
What is a Card Issuance Provider? And who would benefit from issuing their own credit cards?
In the constantly evolving landscape of financial services, card issuers are playing an increasingly important role. But what exactly does a card issuance provider do? Simply put, these entities are the engine behind the creation and management of payment cards. They offer businesses the tools to launch their own branded cards, either physically or digitally, opening up new opportunities for revenue, customer engagement, and financial management.
CaaS
5 min read
Could Your Company Issue Credit Cards? 3 Industries That Could Benefit from Cards-as-a-Service
If you’re looking to expand your profit margins, and your customer base, by adding financial services to your portfolio, a credit card issued and branded by your company is certainly a goal to aspire to. However, without any experience of offering financial services, you might be wondering about the best way to issue credit cards and bolster your revenue streams. Fortunately, Cards-as-a-Service (CaaS) is the simple, effective option that brings your own card program within reach. Let’s look at the industries best suited to issue credit cards and whether your company could benefit too.
CaaS
9 min read
What Is Embedded Finance? And How Could It Benefit Your Business?
As TechCrunch so aptly put it, embedded finance is having a moment: banking, payments, and more are being continually integrated into the apps and platforms you already use. You’d be forgiven for thinking that controlling the means of payment would be a goldmine because, well… it is. In fact, more companies than ever are aiming to blur the lines between product and payment, and if you’ve found this post, yours might be among them.
CaaS
11 min read
All blog posts
Payment Apps
Discover Payment Apps
Real-time monitoring
Receipt management
Spend control
Accounting automations
Multi-currency accounts
Benefits
Integrations
Pro API
Discover Pliant Pro API
Card issuance & management
Transaction insights
Accounting optimization
Member management
Integrations
Custom integrations
CaaS
Discover Cards-as-a-Service
Card issuance & management
Advanced data capabilities
Ready-made UI
Compliance & security
Dedicated support
CaaS API
Integrations
Card OS
Discover Card OS
Accounting automation & integrations
Next-generation financial infrastructure
Modular architecture & detailed customization
Scalable back-office tools
Flexible integration
Cards
Physical cards
Premium cards
Virtual cards
Single-use cards
Travel purchasing cards
Fleet cards
Benefit cards
Insurance claim cards
Solutions
Corporations
E-commerce
Marketing agencies
Resellers
SaaS
Travel
ERP
Invoice management
Travel expense management
Specialised lending
Insurance payments
Customer stories
Resources
Pricing
Help center
Blog
Events
API Documentation
Exchange rates
FAQ
Developers
Company
About Pliant
Careers
HIRING
Press
Contact
Follow us on
linkedin
Pliant''s Youtube channel
Download on the App Store
Download Pliant App on the Google Play Store
© 2020 –
2026
Pliant GmbH
© 2020 –
2026
Pliant GmbH
Pliant is certified as a
Payment Card Industry (PCI) Data Security Standard
service provider and has achieved
ISO Certificate 27001-2022.
Pliant is a financial technology company, not an FDIC Insured bank. Banking services provided by Coastal Community Bank, Member FDIC. FDIC insurance only covers the failure of an FDIC insured bank. FDIC insurance is available through pass-through insurance at Coastal Community Bank, Member FDIC, if certain conditions have been met. The Pliant Corporate Credit Card is issued by Coastal Community Bank, Member FDIC pursuant to a license from Visa U.S.A and may be used everywhere Visa is accepted.
Imprint
Privacy Policy
Coastal Community Bank Privacy Policy
Privacy Settings
Global (English)', '1f3cc1170dfe5d96613a9f051834afb53c0b6d0bc2b1092d42df041ead33f070', '{"url": "https://www.getpliant.com/en-us/blog/what-is-gobd/", "title": "Is your business GoBD compliant?", "accessed_at": "2026-01-16T21:23:10.803253+00:00", "status_code": 200, "content_type": "text/html; charset=utf-8"}', '2026-01-16T21:23:10.804413+00:00');

-- 43 source(s) exported

-- ============================================================================
-- CITATIONS (citation_tool)
-- ============================================================================

INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (1, 'Evidence from document: BETREFF Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unte...', 'BETREFF Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff (GoBD)', 'BETREFF Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff (GoBD)', NULL, 'Direct quote from source document supporting: Evidence from document: BETREFF Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern,', 'high', 'direct_quote', 1, '{"page": 1}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T16:59:02.283848+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (2, 'Evidence from document: Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKEIT", "ALLGEMEINE ANFORDE...', 'Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKEIT", "ALLGEMEINE ANFORDERUNGEN", etc.', 'Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKEIT", "ALLGEMEINE ANFORDERUNGEN", etc.', NULL, 'Direct quote from source document supporting: Evidence from document: Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKE', 'high', 'direct_quote', 1, '{"page": 2}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T16:59:11.661609+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (3, 'Evidence from document: GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unter...', 'GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff', 'GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff', NULL, 'Direct quote from source document supporting: Evidence from document: GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, ', 'high', 'direct_quote', 1, '{"page": 1}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T16:59:31.990548+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (4, 'Evidence from document: Data processing system (DV-System) described in page 6...', 'Data processing system (DV-System) described in page 6', 'Data processing system (DV-System) described in page 6', NULL, 'Direct quote from source document supporting: Evidence from document: Data processing system (DV-System) described in page 6...', 'high', 'direct_quote', 1, '{"page": 6}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T16:59:38.771627+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (5, 'Evidence from document: Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKEIT", "ALLGEMEINE ANFORDE...', 'Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKEIT", "ALLGEMEINE ANFORDERUNGEN", etc.', 'Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKEIT", "ALLGEMEINE ANFORDERUNGEN", etc.', NULL, 'Direct quote from source document supporting: Evidence from document: Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKE', 'high', 'direct_quote', 1, '{"page": 2}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T16:59:47.435944+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (6, 'Evidence from document: Section 1.1 and 1.2 describe tax-related bookkeeping obligations and references to §§ 238 ff. HGB an...', 'Section 1.1 and 1.2 describe tax-related bookkeeping obligations and references to §§ 238 ff. HGB and §§ 140 AO.', 'Section 1.1 and 1.2 describe tax-related bookkeeping obligations and references to §§ 238 ff. HGB and §§ 140 AO.', NULL, 'Direct quote from source document supporting: Evidence from document: Section 1.1 and 1.2 describe tax-related bookkeeping obligations and referen', 'high', 'direct_quote', 1, '{"page": 4}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T16:59:55.960585+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (7, 'Evidence from document: Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen i...', 'Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff (GoBD)', 'Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff (GoBD)', NULL, 'Direct quote from source document supporting: Evidence from document: Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeic', 'high', 'direct_quote', 1, '{"page": 1}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:00:49.726814+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (8, 'Evidence from document: BETREFF Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unte...', 'BETREFF Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff (GoBD)', 'BETREFF Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff (GoBD)', NULL, 'Direct quote from source document supporting: Evidence from document: BETREFF Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern,', 'high', 'direct_quote', 1, '{"page": 1}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:01:08.637389+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (9, 'Evidence from document: BETREFF Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unte...', 'BETREFF Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff (GoBD)', 'BETREFF Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff (GoBD)', NULL, 'Direct quote from source document supporting: Evidence from document: BETREFF Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern,', 'high', 'direct_quote', 1, '{"page": 1}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:01:20.720211+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (10, 'Evidence from document: Title: Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unter...', 'Title: Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff (GoBD)', 'Title: Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff (GoBD)', NULL, 'Direct quote from source document supporting: Evidence from document: Title: Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, ', 'high', 'direct_quote', 1, '{"page": 1}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:01:28.087475+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (11, 'Evidence from document: GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unter...', 'GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff', 'GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff', NULL, 'Direct quote from source document supporting: Evidence from document: GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, ', 'high', 'direct_quote', 1, '{"page": 1}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:01:40.205596+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (12, 'Evidence from document: GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unter...', 'GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff', 'GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff', NULL, 'Direct quote from source document supporting: Evidence from document: GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, ', 'high', 'direct_quote', 1, '{"page": 1}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:02:14.085119+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (13, 'Evidence from document: GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unter...', 'GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff', 'GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff', NULL, 'Direct quote from source document supporting: Evidence from document: GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, ', 'high', 'direct_quote', 1, '{"page": 1}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:02:27.995765+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (14, 'Evidence from document: GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unter...', 'GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff', 'GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff', NULL, 'Direct quote from source document supporting: Evidence from document: GoBD – Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, ', 'high', 'direct_quote', 1, '{"page": 1}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:02:55.140874+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (15, 'Evidence from document: Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKEIT", "ALLGEMEINE ANFORDE...', 'Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKEIT", "ALLGEMEINE ANFORDERUNGEN", etc.', 'Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKEIT", "ALLGEMEINE ANFORDERUNGEN", etc.', NULL, 'Direct quote from source document supporting: Evidence from document: Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKE', 'high', 'direct_quote', 1, '{"page": 2}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:03:06.697319+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (16, 'Evidence from document: Reference to §§ 238 ff. HGB and §§ 140 AO, as well as other tax laws (UStG, EStG) in section 1.1...', 'Reference to §§ 238 ff. HGB and §§ 140 AO, as well as other tax laws (UStG, EStG) in section 1.1', 'Reference to §§ 238 ff. HGB and §§ 140 AO, as well as other tax laws (UStG, EStG) in section 1.1', NULL, 'Direct quote from source document supporting: Evidence from document: Reference to §§ 238 ff. HGB and §§ 140 AO, as well as other tax laws (UStG, ', 'high', 'direct_quote', 1, '{"page": 4}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:03:16.122894+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (17, 'Evidence from document: Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKEIT", "ALLGEMEINE ANFORDE...', 'Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKEIT", "ALLGEMEINE ANFORDERUNGEN", etc.', 'Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKEIT", "ALLGEMEINE ANFORDERUNGEN", etc.', NULL, 'Direct quote from source document supporting: Evidence from document: Table of contents includes sections such as "ALLGEMEINES", "VERANTWORTLICHKE', 'high', 'direct_quote', 1, '{"page": 2}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:03:25.703574+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (18, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:10:05.678376+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (19, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:10:08.958874+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (20, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:10:36.932433+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (21, 'Evidence from document: Branch‑specific minimal recording obligations and reasonableness must be considered....', 'Branch‑specific minimal recording obligations and reasonableness must be considered.', 'Branch‑specific minimal recording obligations and reasonableness must be considered.', NULL, 'Direct quote from source document supporting: Evidence from document: Branch‑specific minimal recording obligations and reasonableness must be con', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:10:56.915523+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (22, 'Evidence from document: The recording of each individual transaction is not required if it is technically, economically, and...', 'The recording of each individual transaction is not required if it is technically, economically, and practically impossible, and the taxpayer must prove this.', 'The recording of each individual transaction is not required if it is technically, economically, and practically impossible, and the taxpayer must prove this.', NULL, 'Direct quote from source document supporting: Evidence from document: The recording of each individual transaction is not required if it is techni', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:11:09.939016+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (23, 'Evidence from document: For cash sales to many unknown persons, the individual recording requirement does not apply if an op...', 'For cash sales to many unknown persons, the individual recording requirement does not apply if an open cash register is used; however, if an electronic recording system is used, the individual recording requirement applies regardless of technical security.', 'For cash sales to many unknown persons, the individual recording requirement does not apply if an open cash register is used; however, if an electronic recording system is used, the individual recording requirement applies regardless of technical security.', NULL, 'Direct quote from source document supporting: Evidence from document: For cash sales to many unknown persons, the individual recording requirement', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:11:43.061283+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (24, 'Evidence from document: The completeness and traceability of all business transactions must be ensured in IT systems through...', 'The completeness and traceability of all business transactions must be ensured in IT systems through technical and organizational controls.', 'The completeness and traceability of all business transactions must be ensured in IT systems through technical and organizational controls.', NULL, 'Direct quote from source document supporting: Evidence from document: The completeness and traceability of all business transactions must be ensur', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:12:07.102646+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (25, 'Evidence from document: Plausibility controls must be implemented for data entry, including content plausibility checks, aut...', 'Plausibility controls must be implemented for data entry, including content plausibility checks, automated assignment of record numbers, gap analysis, and duplicate analysis for document numbers.', 'Plausibility controls must be implemented for data entry, including content plausibility checks, automated assignment of record numbers, gap analysis, and duplicate analysis for document numbers.', NULL, 'Direct quote from source document supporting: Evidence from document: Plausibility controls must be implemented for data entry, including content ', 'high', 'direct_quote', 1, '{"page": 12}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:12:22.490723+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (26, 'Evidence from document: A single business transaction must not be recorded multiple times....', 'A single business transaction must not be recorded multiple times.', 'A single business transaction must not be recorded multiple times.', NULL, 'Direct quote from source document supporting: Evidence from document: A single business transaction must not be recorded multiple times....', 'high', 'direct_quote', 1, '{"page": 12}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:12:48.696555+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (27, 'Evidence from document: Summarized or aggregated records in the general ledger are permissible only if they can be traced ba...', 'Summarized or aggregated records in the general ledger are permissible only if they can be traced back to individual entries in the underlying records.', 'Summarized or aggregated records in the general ledger are permissible only if they can be traced back to individual entries in the underlying records.', NULL, 'Direct quote from source document supporting: Evidence from document: Summarized or aggregated records in the general ledger are permissible only ', 'high', 'direct_quote', 1, '{"page": 12}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:13:15.828198+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (28, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:13:27.582357+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (29, 'Evidence from document: Branch‑specific minimal recording obligations and reasonableness must be considered....', 'Branch‑specific minimal recording obligations and reasonableness must be considered.', 'Branch‑specific minimal recording obligations and reasonableness must be considered.', NULL, 'Direct quote from source document supporting: Evidence from document: Branch‑specific minimal recording obligations and reasonableness must be con', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:13:59.074533+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (30, 'Evidence from document: The recording or processing of actual business transactions must not be suppressed; e.g., a receipt ...', 'The recording or processing of actual business transactions must not be suppressed; e.g., a receipt or invoice must not be issued without recording cash received.', 'The recording or processing of actual business transactions must not be suppressed; e.g., a receipt or invoice must not be issued without recording cash received.', NULL, 'Direct quote from source document supporting: Evidence from document: The recording or processing of actual business transactions must not be supp', 'high', 'direct_quote', 1, '{"page": 12}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:14:11.174218+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (31, 'Evidence from document: The recording of each individual transaction is not required if it is technically, economically, and...', 'The recording of each individual transaction is not required if it is technically, economically, and practically impossible, and the taxpayer must prove this.', 'The recording of each individual transaction is not required if it is technically, economically, and practically impossible, and the taxpayer must prove this.', NULL, 'Direct quote from source document supporting: Evidence from document: The recording of each individual transaction is not required if it is techni', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:14:28.418145+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (32, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:14:44.048267+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (33, 'Evidence from document: For cash sales to many unknown persons, the individual recording requirement does not apply if an op...', 'For cash sales to many unknown persons, the individual recording requirement does not apply if an open cash register is used; however, if an electronic recording system is used, the individual recording requirement applies regardless of technical security.', 'For cash sales to many unknown persons, the individual recording requirement does not apply if an open cash register is used; however, if an electronic recording system is used, the individual recording requirement applies regardless of technical security.', NULL, 'Direct quote from source document supporting: Evidence from document: For cash sales to many unknown persons, the individual recording requirement', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:15:22.376446+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (34, 'Evidence from document: The completeness and traceability of all business transactions must be ensured in IT systems through...', 'The completeness and traceability of all business transactions must be ensured in IT systems through technical and organizational controls.', 'The completeness and traceability of all business transactions must be ensured in IT systems through technical and organizational controls.', NULL, 'Direct quote from source document supporting: Evidence from document: The completeness and traceability of all business transactions must be ensur', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:15:39.021853+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (35, 'Evidence from document: Plausibility controls must be implemented for data entry, including content plausibility checks, aut...', 'Plausibility controls must be implemented for data entry, including content plausibility checks, automated assignment of record numbers, gap analysis, and duplicate analysis for document numbers.', 'Plausibility controls must be implemented for data entry, including content plausibility checks, automated assignment of record numbers, gap analysis, and duplicate analysis for document numbers.', NULL, 'Direct quote from source document supporting: Evidence from document: Plausibility controls must be implemented for data entry, including content ', 'high', 'direct_quote', 1, '{"page": 12}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:16:41.810625+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (36, 'Evidence from document: A single business transaction must not be recorded multiple times....', 'A single business transaction must not be recorded multiple times.', 'A single business transaction must not be recorded multiple times.', NULL, 'Direct quote from source document supporting: Evidence from document: A single business transaction must not be recorded multiple times....', 'high', 'direct_quote', 1, '{"page": 12}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:17:06.808392+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (37, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:17:22.690326+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (38, 'Evidence from document: Branch‑specific minimal recording obligations and reasonableness must be considered....', 'Branch‑specific minimal recording obligations and reasonableness must be considered.', 'Branch‑specific minimal recording obligations and reasonableness must be considered.', NULL, 'Direct quote from source document supporting: Evidence from document: Branch‑specific minimal recording obligations and reasonableness must be con', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:18:01.431211+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (39, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:18:05.453076+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (40, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:18:36.424146+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (41, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:19:10.135841+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (42, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:19:28.986349+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (43, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:20:04.481974+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (44, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:20:20.327255+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (45, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:20:38.509662+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (46, 'Evidence from document: Plausibility controls must be implemented for data entry, including content plausibility checks, aut...', 'Plausibility controls must be implemented for data entry, including content plausibility checks, automated assignment of record numbers, gap analysis, and duplicate analysis for document numbers.', 'Plausibility controls must be implemented for data entry, including content plausibility checks, automated assignment of record numbers, gap analysis, and duplicate analysis for document numbers.', NULL, 'Direct quote from source document supporting: Evidence from document: Plausibility controls must be implemented for data entry, including content ', 'high', 'direct_quote', 1, '{"page": 12}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:20:43.797533+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (47, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:20:59.470663+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (48, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:21:31.674539+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (49, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:21:51.638672+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (50, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:22:03.661007+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (51, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:22:28.905279+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (52, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:22:43.346520+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (53, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:23:13.545952+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (54, 'Evidence from document: Plausibility controls must be implemented for data entry, including content plausibility checks, aut...', 'Plausibility controls must be implemented for data entry, including content plausibility checks, automated assignment of record numbers, gap analysis, and duplicate analysis for document numbers.', 'Plausibility controls must be implemented for data entry, including content plausibility checks, automated assignment of record numbers, gap analysis, and duplicate analysis for document numbers.', NULL, 'Direct quote from source document supporting: Evidence from document: Plausibility controls must be implemented for data entry, including content ', 'high', 'direct_quote', 1, '{"page": 12}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:23:19.863273+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (55, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:23:40.312850+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (56, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:23:51.509304+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (57, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:24:05.241614+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (58, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:24:26.027245+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (59, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:24:46.126512+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (60, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:25:18.855359+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (61, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:25:32.919906+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (62, 'Requirement', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Requirement', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:25:41.017385+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (63, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:25:55.521673+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (64, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:26:14.256067+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (65, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:26:38.928300+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (66, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:26:51.912357+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (67, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:27:12.318765+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (68, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:27:32.185380+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (69, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:28:07.282526+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (70, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:28:26.124221+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (71, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:28:43.949480+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (72, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:29:01.637203+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (73, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:29:14.391447+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (74, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:29:43.457514+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (75, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:30:06.559530+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (76, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:30:23.542031+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (77, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:30:44.204227+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (78, 'Evidence from document: The entrepreneur must record each business transaction with sufficient detail, including the name of...', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', 'The entrepreneur must record each business transaction with sufficient detail, including the name of the contract partner, where feasible.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record each business transaction with sufficient detai', 'high', 'direct_quote', 1, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:30:50.502486+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (79, 'Evidence from document: The entrepreneur must record for each transaction the unit price, valuation date, discount informati...', 'The entrepreneur must record for each transaction the unit price, valuation date, discount information, payment method, and tax exemption details.', 'The entrepreneur must record for each transaction the unit price, valuation date, discount information, payment method, and tax exemption details.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record for each transaction the unit price, valuation ', 'high', 'direct_quote', 1, '{"page": 21}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:31:06.842486+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (80, 'Evidence from document: The entrepreneur must record for each transaction the unit price, valuation date, discount informati...', 'The entrepreneur must record for each transaction the unit price, valuation date, discount information, payment method, and tax exemption details.', 'The entrepreneur must record for each transaction the unit price, valuation date, discount information, payment method, and tax exemption details.', NULL, 'Direct quote from source document supporting: Evidence from document: The entrepreneur must record for each transaction the unit price, valuation ', 'high', 'direct_quote', 1, '{"page": 21}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:41:37.766844+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (81, 'Evidence from document: Mathematisch‑technische Auswertungen müssen automatisiert (DV‑gestützt) interpretiert, dargestellt, ...', 'Mathematisch‑technische Auswertungen müssen automatisiert (DV‑gestützt) interpretiert, dargestellt, verarbeitet und für andere Datenbank‑anwendungen und Prüfsoftware nutzbar gemacht werden, ohne weitere Konvertierungs‑ und Bearbeitungsschritte und ohne Informationsverlust.', 'Mathematisch‑technische Auswertungen müssen automatisiert (DV‑gestützt) interpretiert, dargestellt, verarbeitet und für andere Datenbank‑anwendungen und Prüfsoftware nutzbar gemacht werden, ohne weitere Konvertierungs‑ und Bearbeitungsschritte und ohne Informationsverlust.', NULL, 'Direct quote from source document supporting: Evidence from document: Mathematisch‑technische Auswertungen müssen automatisiert (DV‑gestützt) inte', 'high', 'direct_quote', 1, '{"page": 31}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T17:56:36.576270+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (82, 'Evidence from document: Mathematisch‑technische Auswertungen sind bei elektronischen Grund(buch)aufzeichnungen, Journaldaten...', 'Mathematisch‑technische Auswertungen sind bei elektronischen Grund(buch)aufzeichnungen, Journaldaten und strukturierten Text‑ bzw. Tabellendateien möglich.', 'Mathematisch‑technische Auswertungen sind bei elektronischen Grund(buch)aufzeichnungen, Journaldaten und strukturierten Text‑ bzw. Tabellendateien möglich.', NULL, 'Direct quote from source document supporting: Evidence from document: Mathematisch‑technische Auswertungen sind bei elektronischen Grund(buch)aufz', 'high', 'direct_quote', 1, '{"page": 31}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T18:01:12.721007+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (83, 'Evidence from document: Ein und derselbe Geschäftsvorfall darf nicht mehrfach aufgezeichnet werden....', 'Ein und derselbe Geschäftsvorfall darf nicht mehrfach aufgezeichnet werden.', 'Ein und derselbe Geschäftsvorfall darf nicht mehrfach aufgezeichnet werden.', NULL, 'Direct quote from source document supporting: Evidence from document: Ein und derselbe Geschäftsvorfall darf nicht mehrfach aufgezeichnet werden..', 'high', 'direct_quote', 2, '{"page": 11}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T19:14:47.814475+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (84, 'Evidence from document: Der Steuerpflichtige hat organisatorisch und technisch sicherzustellen, dass die elektronischen Buch...', 'Der Steuerpflichtige hat organisatorisch und technisch sicherzustellen, dass die elektronischen Buchungen und sonst erforderlichen elektronischen Aufzeichnungen einzeln, vollständig, richtig, zeitgerecht und geordnet vorgenommen werden (§ 146 Absatz 1 Satz 1 AO, § 239 Absatz 2 HGB).', 'Der Steuerpflichtige hat organisatorisch und technisch sicherzustellen, dass die elektronischen Buchungen und sonst erforderlichen elektronischen Aufzeichnungen einzeln, vollständig, richtig, zeitgerecht und geordnet vorgenommen werden (§ 146 Absatz 1 Satz 1 AO, § 239 Absatz 2 HGB).', NULL, 'Direct quote from source document supporting: Evidence from document: Der Steuerpflichtige hat organisatorisch und technisch sicherzustellen, dass', 'high', 'direct_quote', 2, '{"page": 21}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T19:24:29.714552+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (85, 'Evidence from document: Bei der doppelten Buchführung müssen alle Geschäftsvorfälle in zeitlicher Reihenfolge (Grund(buch)au...', 'Bei der doppelten Buchführung müssen alle Geschäftsvorfälle in zeitlicher Reihenfolge (Grund(buch)aufzeichnung, Journalfunktion) und in sachlicher Gliederung (Hauptbuch, Kontenfunktion) darstellbar sein.', 'Bei der doppelten Buchführung müssen alle Geschäftsvorfälle in zeitlicher Reihenfolge (Grund(buch)aufzeichnung, Journalfunktion) und in sachlicher Gliederung (Hauptbuch, Kontenfunktion) darstellbar sein.', NULL, 'Direct quote from source document supporting: Evidence from document: Bei der doppelten Buchführung müssen alle Geschäftsvorfälle in zeitlicher Re', 'high', 'direct_quote', 2, '{"page": 22}', 'failed', 'Verification error: Error code: 404 - {''error'': {''message'': ''The model `gpt-4o-mini` does not exist.'', ''type'': ''NotFoundError'', ''param'': None, ''code'': 404}}', 0.0, NULL, '2026-01-16T19:36:21.153769+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (86, 'Evidence from document: Der Unternehmer muss die Aufbewahrung von steuerlich relevanten Unterlagen für 10 Jahre sicherstelle...', 'Der Unternehmer muss die Aufbewahrung von steuerlich relevanten Unterlagen für 10 Jahre sicherstellen.', 'Der Unternehmer muss die Aufbewahrung von steuerlich relevanten Unterlagen für 10 Jahre sicherstellen.', NULL, 'Direct quote from source document supporting: Evidence from document: Der Unternehmer muss die Aufbewahrung von steuerlich relevanten Unterlagen f', 'high', 'direct_quote', 3, '{"page": 12}', 'failed', 'The source document contains extensive discussion of the obligations to retain business records and references to the "Aufbewahrungsfrist" (retention period), but the exact sentence "Der Unternehmer muss die Aufbewahrung von steuerlich relevanten Unterlagen für 10 Jahre sicherstellen." does not appear anywhere in the provided pages. No passage explicitly states a 10‑year retention period for tax‑relevant documents, so the quoted text cannot be found and does not support the claim.', 0.07000000029802322, NULL, '2026-01-16T21:04:10.359869+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (87, 'Evidence from document: 3.2.2 Richtigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB)...', '3.2.2 Richtigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB)', '3.2.2 Richtigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB)', NULL, 'Direct quote from source document supporting: Evidence from document: 3.2.2 Richtigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB)...', 'high', 'direct_quote', 3, '{"page": 12}', 'verified', 'The exact phrase "3.2.2 Richtigkeit (§ 146 Absatz 1 AO, § 239 Absatz 2 HGB)" appears in the source document (see page 12, heading for section 3.2.2). This directly confirms the quoted text and therefore supports the claim that the document contains this evidence.', 1.0, '{"line": 44, "page": 12}', '2026-01-16T21:04:46.900311+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (88, 'Evidence from document: Alle für die maschinelle Auswertung notwendigen Strukturinformationen (Dateiherkunft, Dateistruktur,...', 'Alle für die maschinelle Auswertung notwendigen Strukturinformationen (Dateiherkunft, Dateistruktur, Datenfelder, Zeichensatztabellen) müssen in maschinell auswertbarer, unverdichteter Form aufbewahrt werden, ebenso interne und externe Verknüpfungen vollständig und in unverdichteter, maschinell auswertbarer Form aufzubewahren.', 'Alle für die maschinelle Auswertung notwendigen Strukturinformationen (Dateiherkunft, Dateistruktur, Datenfelder, Zeichensatztabellen) müssen in maschinell auswertbarer, unverdichteter Form aufbewahrt werden, ebenso interne und externe Verknüpfungen vollständig und in unverdichteter, maschinell auswertbarer Form aufzubewahren.', NULL, 'Direct quote from source document supporting: Evidence from document: Alle für die maschinelle Auswertung notwendigen Strukturinformationen (Datei', 'high', 'direct_quote', 3, '{"page": 31}', 'failed', 'The exact quoted sentence does not appear anywhere in the provided source content (pages 1‑21 of the GoBD document). A search of the text shows no occurrence of the phrase "Alle für die maschinelle Auswertung notwendigen Strukturinformationen (Dateiherkunft, Dateistruktur, Datenfelder, Zeichensatztabellen) ..." or any close paraphrase. Therefore the citation does not exist in the source and cannot support the claim.', 0.0, NULL, '2026-01-16T21:05:42.957772+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (89, 'Evidence from document: Alle für die maschinelle Auswertung notwendigen Strukturinformationen (Dateiherkunft, Dateistruktur,...', 'Alle für die maschinelle Auswertung notwendigen Strukturinformationen (Dateiherkunft, Dateistruktur, Datenfelder, verwendete Zeichensatztabellen)', 'Alle für die maschinelle Auswertung notwendigen Strukturinformationen (Dateiherkunft, Dateistruktur, Datenfelder, verwendete Zeichensatztabellen)', NULL, 'Direct quote from source document supporting: Evidence from document: Alle für die maschinelle Auswertung notwendigen Strukturinformationen (Datei', 'high', 'direct_quote', 3, '{"page": 31}', 'failed', 'The source document (the GoBD guidelines) does not contain the phrase "Alle für die maschinelle Auswertung notwendigen Strukturinformationen (Dateiherkunft, Dateistruktur, Datenfelder, verwendete Zeichensatztabellen)" or any close paraphrase. The text deals with tax bookkeeping requirements and only mentions "Maschinelle Auswertbarkeit" in a different context, but the specific quoted sentence is absent. Therefore the citation does not exist in the source and cannot support the claim.', 0.0, NULL, '2026-01-16T21:06:54.093017+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (90, 'Evidence from document: Alle für die maschinelle Auswertung notwendigen Strukturinformationen...', 'Alle für die maschinelle Auswertung notwendigen Strukturinformationen', 'Alle für die maschinelle Auswertung notwendigen Strukturinformationen', NULL, 'Direct quote from source document supporting: Evidence from document: Alle für die maschinelle Auswertung notwendigen Strukturinformationen...', 'high', 'direct_quote', 3, '{"page": 31}', 'failed', 'The exact phrase "Alle für die maschinelle Auswertung notwendigen Strukturinformationen" does not appear anywhere in the provided source content. The document contains related terms such as "MASCHINELLE AUSWERTBARKEIT" and mentions of "maschinell auswertbarer Form", but the quoted text is not present and therefore cannot support the claim.', 0.0, NULL, '2026-01-16T21:08:34.192587+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (91, 'Evidence from document: Maschinell auswertbarer...', 'Maschinell auswertbarer', 'Maschinell auswertbarer', NULL, 'Direct quote from source document supporting: Evidence from document: Maschinell auswertbarer...', 'high', 'direct_quote', 3, '{"page": 31}', 'failed', 'The source contains the noun phrase "MASCHINELLE AUSWERTBARKEIT" (page 3, section 9.1) but does not contain the exact quoted phrase "Maschinell auswertbarer". The wording differs (noun vs. adjective form) and therefore the quoted text is not present, nor does it directly support the claim as stated.', 0.3199999928474426, '{"page": 3, "section": "9.1"}', '2026-01-16T21:08:45.016286+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (92, 'Evidence from document: Maschinell auswertbarer Form...', 'Maschinell auswertbarer Form', 'Maschinell auswertbarer Form', NULL, 'Direct quote from source document supporting: Evidence from document: Maschinell auswertbarer Form...', 'high', 'direct_quote', 3, '{"page": 31}', 'failed', 'The source contains the term "MASCHINELLE AUSWERTBARKEIT" (machine‑readability) but does not contain the exact phrase "Maschinell auswertbarer Form". The wording differs (noun vs. adjective phrase) and therefore the quoted text is not found verbatim in the source, nor does it directly support the claim as stated.', 0.41999998688697815, '{"page": 9, "section": "9.1"}', '2026-01-16T21:09:26.824469+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (93, 'Evidence from document: Maschinelle Auswertbarkeit...', 'Maschinelle Auswertbarkeit', 'Maschinelle Auswertbarkeit', NULL, 'Direct quote from source document supporting: Evidence from document: Maschinelle Auswertbarkeit...', 'high', 'direct_quote', 3, '{"page": 31}', 'verified', 'The source document contains the heading ''MASCHINELLE AUSWERTBARKEIT'' (page 3, section 9.1). This matches the quoted phrase ''Maschinelle Auswertbarkeit'' (case‑insensitive) and directly provides the evidence that the document mentions this term, thereby supporting the claim.', 0.9800000190734863, '{"page": 3, "section": "9.1"}', '2026-01-16T21:09:45.185694+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (94, 'Data retention period of ten years', 'GoBD requires electronic documents to be retained for ten years.', 'GoBD requires electronic documents to be retained for ten years.', NULL, 'Content from web source supporting: Data retention period of ten years', 'high', 'direct_quote', 4, '{"title": "GoBD - Principles for electronic accounting - TeamDrive", "accessed_at": "2026-01-16"}', 'verified', 'The source does not contain the exact verbatim phrase, but it includes statements that electronic documents (electronically received documents and data) must be retained for ten years. This meaning aligns with the claim that GoBD requires electronic documents to be retained for ten years, so the citation supports the claim despite minor paraphrasing.', 0.8600000143051147, '{"section": "Storage", "paragraph": "Electronically received documents and data are subject to a ten-year retention period. Business documents in the form of e‑mails must be digitally archived for six to ten years. ... These documents must be retained for ten years"}', '2026-01-16T21:12:19.885570+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (95, 'Information from web source: GoBD mandates an audit trail that captures who made changes, when, and why....', 'GoBD mandates an audit trail that captures who made changes, when, and why.', 'GoBD mandates an audit trail that captures who made changes, when, and why.', NULL, 'Content from web source supporting: Information from web source: GoBD mandates an audit trail that captures who made changes, when, and ', 'high', 'direct_quote', 5, '{"title": "What is GoBD? | Hornetsecurity", "accessed_at": "2026-01-16"}', 'failed', 'The exact verbatim quote "GoBD mandates an audit trail that captures who made changes, when, and why." does not appear anywhere in the provided Hornetsecurity Knowledge Base article. The article discusses related concepts such as immutability, the need to record commit times, and that auditors may request activity logs, but it never uses the phrase "audit trail" nor states that GoBD explicitly mandates an audit trail capturing who, when, and why. Therefore the citation does not directly support the claim as quoted.', 0.18000000715255737, NULL, '2026-01-16T21:13:58.173821+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');
INSERT INTO citations ("id", "claim", "verbatim_quote", "quote_context", "quote_language", "relevance_reasoning", "confidence", "extraction_method", "source_id", "locator", "verification_status", "verification_notes", "similarity_score", "matched_location", "created_at", "created_by") VALUES (96, 'Information from web source: Electronic invoices must be retained for ten years in a format that ensures auditability and data in...', 'Electronic invoices must be retained for ten years in a format that ensures auditability and data integrity.', 'Electronic invoices must be retained for ten years in a format that ensures auditability and data integrity.', NULL, 'Content from web source supporting: Information from web source: Electronic invoices must be retained for ten years in a format that ens', 'high', 'direct_quote', 25, '{"title": "Germany updates GoBD rules for 2025 e-invoicing mandate - Dynatos", "accessed_at": "2026-01-16"}', 'failed', 'The source content discusses the German GoBD amendment and mentions that the original file format of an e‑invoice must be stored and that the XML component is the legally relevant element. It does not contain the phrase "Electronic invoices must be retained for ten years in a format that ensures auditability and data integrity" nor any statement specifying a ten‑year retention period or auditability/data‑integrity requirements. Therefore the quoted text is not found in the source and does not support the claim.', 0.11999999731779099, NULL, '2026-01-16T21:15:58.116006+00:00', 'bad7b675-da31-439f-a32e-e9505c8ab308:creator');

-- 96 citation(s) exported
