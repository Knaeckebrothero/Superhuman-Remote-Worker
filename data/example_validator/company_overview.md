# FINIUS GmbH - Company Overview

> This document provides business context for the FINIUS car rental company. Use this information when extracting and validating requirements to understand the domain, processes, and compliance obligations.

## Company Profile

FINIUS GmbH (operating as FiniServRental) is a German car rental and mobility services company. The company operates multiple rental stations serving both private customers (Privatkunden) and corporate accounts (Geschäftskunden).

**Locations:**
- Airport Station (Flughafen) - High-volume location for travelers
- Downtown Station (City) - City center location for local rentals
- Fleet & Maintenance Station - Central hub for vehicle maintenance
- CCS Car Clean Service - Dedicated cleaning and detailing

## Core Business: The Rental Lifecycle

FINIUS follows a four-stage value stream for its mobility products:

### 1. Book Mobility (Mobilität buchen)
Customer requests a rental. Staff checks vehicle availability, creates a price offer (Angebot), and confirms the reservation. Insurance options and add-on services are offered. Cancellations (Storno) are handled with appropriate policies.

### 2. Start Mobility (Mobilität beginnen)
Vehicle is prepared for handover: cleaned, fueled, and inspected. Customer receives the vehicle with documentation of its condition. A security deposit (Kaution) or payment pre-authorization (Pre-Auth) is collected.

### 3. Use Vehicle (Wagen nutzen)
Customer uses the rental car. Customer service handles any issues that arise during the rental period, including breakdowns, accidents, or booking changes.

### 4. End Mobility (Mobilität beenden)
Customer returns the vehicle. Staff documents the condition, notes any damage, and processes the return. The vehicle is cleaned and returned to the available fleet. A final invoice (Endabrechnung) is created and sent.

## Products & Services

**Rental Tiers:**
- Mobility Basic - Standard rental package
- Mobility Premium - Enhanced service level with priority support

**Insurance Options:**
- Haftpflichtversicherung - Third-party liability (mandatory)
- Vollkaskoversicherung - Comprehensive/collision damage waiver (CDW)
- Insassenversicherung - Passenger accident insurance
- Glass coverage - Windshield and window protection

**Add-On Services:**
- Fuel-up service (Auftank Service)
- Professional cleaning (Professionelle Reinigung)
- Delivery and pick-up (Bring und Abholservice)
- Shuttle transfers
- Dedicated corporate account management

## Key Business Objects

When extracting requirements, these are the core entities in FINIUS operations:

| Object | German | Description |
|--------|--------|-------------|
| Booking | Buchung | Rental agreement with dates, vehicle, and customer |
| Offer | Angebot | Price quote for a potential rental |
| Invoice | Rechnung | Billing document for completed services |
| Credit Note | Gutschrift | Refund or adjustment document |
| Payment | Zahlung | Transaction record (card, SEPA, cash) |
| Vehicle | Fahrzeug | Fleet asset with category, status, maintenance history |
| Customer | Kunde | Renter profile with contact and payment details |
| Insurance Claim | Schadensfall | Incident report linked to a booking |

## Compliance Requirements

### GoBD (German Accounting Standards)
FINIUS must comply with the "Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff" - German principles for proper bookkeeping and record retention.

**Key obligations:**
- **Retention (Aufbewahrung):** Invoices, contracts, and accounting records must be retained for 10 years
- **Immutability (Unveränderbarkeit):** Stored documents cannot be modified after creation
- **Traceability (Nachvollziehbarkeit):** Every transaction must be traceable from origin to final record
- **Completeness (Vollständigkeit):** All business transactions must be recorded without gaps
- **Audit trail:** System must maintain logs for tax authority inspection

### GDPR (Data Protection)
As a company handling personal data of customers and employees:
- Explicit consent required for data processing
- Data minimization - collect only what's necessary
- Defined retention periods with automatic deletion
- Customer right to access, correction, and erasure
- Breach notification within 72 hours

## Domain Glossary

| German | English | Context |
|--------|---------|---------|
| Mietwagen | Rental car | The product |
| Buchung | Booking | Reservation/contract |
| Rechnung | Invoice | Billing document |
| Beleg | Receipt/voucher | Proof of transaction |
| Storno | Cancellation | Booking cancellation |
| Kaution | Security deposit | Damage protection |
| Endabrechnung | Final invoice | Settlement at return |
| Mahnwesen | Collections | Overdue payment handling |
| Debitoren | Accounts receivable | Customer debts |
| Kreditoren | Accounts payable | Supplier debts |
| Buchungssatz | Journal entry | Accounting record |
| Steuerberechnung | Tax calculation | VAT computation |
