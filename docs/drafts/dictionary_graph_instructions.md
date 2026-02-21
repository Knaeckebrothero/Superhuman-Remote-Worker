# Dictionary-Seeded Knowledge Graph for Recycling Chatbot

## Goal

Build a bilingual (German + English) seed graph in Neo4j containing physical objects, materials, and substances — the kind of things people might need to dispose of. The graph provides canonical terms, synonyms, translations, and a taxonomic hierarchy. It will later be enriched with waste disposal rules from 500+ sources, but that is NOT part of this job.

This job produces the **skeleton**: clean, deduplicated noun nodes with synonym/translation/taxonomy edges, sourced from structured dictionaries rather than LLM-generated text.

## Neo4j Schema

### Node Labels

| Label | Properties | Description |
|-------|-----------|-------------|
| `:Item` | `name`, `lang`, `pos`, `source` | A physical object, material, or substance (canonical term) |
| `:Category` | `name`, `lang`, `level` | Taxonomic grouping (e.g., Lebensmittel, Elektronik, Chemikalie) |

### Relationship Types

| Type | Direction | Description |
|------|-----------|-------------|
| `SYNONYM_OF` | `(:Item)-[:SYNONYM_OF]->(:Item)` | Synonym link (bidirectional semantically, stored once) |
| `TRANSLATION_OF` | `(:Item)-[:TRANSLATION_OF {lang_from, lang_to}]->(:Item)` | Cross-language equivalent |
| `IS_A` | `(:Item)-[:IS_A]->(:Item)` or `(:Item)-[:IS_A]->(:Category)` | Hypernym / "is a kind of" |
| `HAS_PART` | `(:Item)-[:HAS_PART]->(:Item)` | Meronym (e.g., Smartphone HAS_PART Batterie) |

### Constraints (create first)

```cypher
CREATE CONSTRAINT item_unique IF NOT EXISTS FOR (i:Item) REQUIRE (i.name, i.lang) IS UNIQUE;
CREATE CONSTRAINT category_unique IF NOT EXISTS FOR (c:Category) REQUIRE (c.name, c.lang) IS UNIQUE;
CREATE INDEX item_name IF NOT EXISTS FOR (i:Item) ON (i.name);
CREATE INDEX item_source IF NOT EXISTS FOR (i:Item) ON (i.source);
```

## Dictionary Sources (Priority Order)

### 1. OdeNet — Open German WordNet (MUST USE)
- Install: `pip install wn` then `python -m wn download odenet:1.4`
- License: CC-BY-SA 4.0
- Content: ~36,000 German synsets with synonyms and hypernym/hyponym trees
- Access: `import wn; de = wn.Wordnet('odenet', '1.4')`
- Extract: All noun synsets → canonical lemma + synonym lemmas + hypernym chain
- This is your primary German source

### 2. Open English WordNet (MUST USE)
- Install: `python -m wn download ewn:2020` (or latest available)
- Content: English nouns with synonym sets, hypernym trees, meronyms
- Access: `import wn; en = wn.Wordnet('ewn', '2020')`
- Use the Collaborative Interlingual Index (ILI) to link German ↔ English synsets:
  ```python
  # OdeNet synsets have ILI keys that map to English WordNet synsets
  for ss in de.synsets(pos='n'):
      ili = ss.ili()  # Interlingual index
      # Find matching English synset via ILI
  ```

### 3. OpenThesaurus (SHOULD USE)
- Download: SQL dump from https://www.openthesaurus.de/about/download
- Content: 280,000+ German synonyms in clusters
- Use to enrich synonym coverage beyond what OdeNet provides
- Cross-reference: match OpenThesaurus clusters to existing OdeNet nodes

### 4. Kaikki.org German JSONL (OPTIONAL — large file)
- Download: German dictionary JSONL from https://kaikki.org/dictionary/German/
- ~933MB, one JSON object per word sense
- Install: `pip install kaikki-json`
- Use to fill gaps: nouns that OdeNet doesn't cover, additional translations
- Filter: `pos == "noun"` entries only

## Filtering Strategy

Not every noun belongs in a recycling graph. Filter to **physical nouns** — things that have mass and could theoretically end up as waste.

### Include
- Objects: Smartphone, Fahrrad, Tasse, Zeitung
- Materials: Plastik, Glas, Holz, Aluminium
- Substances: Farbe, Öl, Säure, Lösungsmittel
- Food: Banane, Brot, Schokolade, Fleisch
- Packaging: Karton, Folie, Dose, Flasche
- Organic matter: Laub, Grasschnitt, Erde

### Exclude
- Abstract concepts: Demokratie, Liebe, Freiheit
- Events: Hochzeit, Konzert
- Pure locations: Berlin, Europa
- Pure persons/roles: Lehrer, Präsident (unless the item form matters, e.g., "Uniform")

### How to filter
Use the WordNet hypernym tree. Walk up from each noun — if it eventually reaches `physical_entity`, `substance`, `artifact`, `food`, `material`, or `organism` (for organic waste), include it. If it reaches `abstraction`, `attribute`, `event`, `state`, exclude it.

For nouns without hypernym data, use a simple LLM classification: "Is [noun] a physical object, material, or substance that could become waste? yes/no". Use `claude_code` to write and run a batch classifier script. Small batches, simple yes/no prompt. Don't overthink this — false positives (airplane in the graph) are harmless; false negatives (missing common waste items) are the real risk.

## Canonical Node Selection

When multiple synonyms form a cluster, one must be the **canonical node** (`:Item`), the rest become `SYNONYM_OF` edges.

**Rule: Pick the most generic, shortest German-language term.**

Example:
- Canonical: `Schokolade` → synonyms: `Schokoriegel`, `Tafelschokolade`, `Praline`
- Canonical: `Batterie` → synonyms: `Knopfzelle`, `Akkumulator`
- Canonical: `Plastik` → synonyms: `Kunststoff`, `Plaste`

When OdeNet provides a synset, use the first lemma as canonical (it's typically the most common form).

## Workflow

### Phase 1: Setup & Source Acquisition
1. Install Python packages: `wn`, `kaikki-json` (optional), `py-openthesaurus` (optional)
2. Download OdeNet: `python -m wn download odenet:1.4`
3. Download Open English WordNet: `python -m wn download ewn:2020`
4. Verify Neo4j connectivity via `get_database_schema`
5. Create constraints and indexes in Neo4j

### Phase 2: Extract German Nouns from OdeNet
1. Write a Python script that extracts all noun synsets from OdeNet
2. For each synset: canonical lemma, all lemmas (synonyms), hypernym chain, ILI key
3. Apply the physical-noun filter using the hypernym tree
4. Save to a structured intermediate file (JSONL or CSV) in `output/`
5. Log statistics: total synsets, filtered count, top-level categories

### Phase 3: Extract English Nouns & Build Translation Links
1. Extract English noun synsets from Open English WordNet
2. Match German ↔ English via ILI keys
3. Apply the same physical-noun filter
4. Save English nouns + translation mappings to intermediate files

### Phase 4: Enrich with OpenThesaurus (if time permits)
1. Download and parse the OpenThesaurus dump
2. For each cluster, try to match to an existing OdeNet canonical node
3. Add new synonyms that OdeNet doesn't have
4. Flag unmatched clusters as potential new nodes

### Phase 5: Import into Neo4j
1. Write a Python import script using the intermediate files
2. Create `:Category` nodes for top-level waste categories
3. Create `:Item` nodes (canonical terms) with properties
4. Create `SYNONYM_OF` edges within synonym clusters
5. Create `TRANSLATION_OF` edges between German and English items
6. Create `IS_A` edges for the taxonomic hierarchy
7. Run the import script via `run_command`
8. Use `execute_cypher_query` to verify node/edge counts

### Phase 6: Verification & Statistics
1. Query total node and relationship counts
2. Spot-check: query 5 common waste items and verify their synonym + translation links
3. Check for orphan nodes (no relationships)
4. Write a summary report to `output/graph_report.md` with:
   - Total Items (German), Total Items (English)
   - Total synonym edges, translation edges, taxonomy edges
   - Top 10 categories by item count
   - Sample subgraphs for 3-5 common items

## Deliverables

| Deliverable | Path | Description |
|-------------|------|-------------|
| Extraction scripts | `output/scripts/` | Python scripts for dictionary extraction and import |
| German nouns (intermediate) | `output/data/german_nouns.jsonl` | Extracted + filtered German physical nouns |
| English nouns (intermediate) | `output/data/english_nouns.jsonl` | Extracted + filtered English physical nouns |
| Translation map | `output/data/translations.jsonl` | German ↔ English mappings via ILI |
| Neo4j graph | (in database) | Populated seed graph |
| Summary report | `output/graph_report.md` | Statistics, sample queries, verification results |

## Using Claude Code for All Coding Work

**Do NOT write Python scripts line-by-line with `write_file`.** That approach is too slow and error-prone for scripts of any complexity.

Instead, use the `claude_code` tool for ALL coding tasks:
- Writing extraction scripts
- Writing import scripts
- Writing filter/classification logic
- Debugging and fixing scripts that fail

Give Claude Code clear instructions:
```
Write a Python script that:
1. Loads OdeNet via the `wn` package
2. Extracts all noun synsets with their lemmas and hypernym chains
3. Filters to physical nouns by walking the hypernym tree
4. Saves results as JSONL to output/data/german_nouns.jsonl
5. Prints statistics (total synsets, filtered count, top categories)

Save the script to output/scripts/extract_german_nouns.py
```

After Claude Code writes the script, execute it with `run_command`:
```
python output/scripts/extract_german_nouns.py
```

If a script fails, delegate the fix back to Claude Code — don't try to patch it yourself with `edit_file`.

Claude Code has its own context window and tools — it can read, write, and execute files directly. This is the fastest and most reliable way to build the pipeline.

## Important Notes

- **Use `execute_cypher_query` for verification**, but use Python scripts (via `run_command`) for bulk import — the Cypher tool is better for spot-checks than importing thousands of nodes one query at a time.
- **Intermediate files matter.** Save extracted data to JSONL files before importing. This makes the pipeline resumable and debuggable.
- **Don't aim for perfection.** A graph with 5,000 clean physical nouns and their synonyms is more valuable than 50,000 nouns with messy relationships. Quality over quantity.
- **The 933MB kaikki.org file is optional.** Start with OdeNet + English WordNet. Only download kaikki.org if you finish the core pipeline and have capacity left.
