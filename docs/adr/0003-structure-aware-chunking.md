# ADR 0003: Structure-aware chunking with heading paths

## Status
Accepted · 4 November 2026 (sizes confirmed by the measurements in docs/PIPELINE.md)

## Context
Our documents are rule books: numbered chapters ("5. Academic Assessment"), titled
sub-sections ("Academic Warning/Probation"), and numbered clauses ("5.4 Minimum CGPA
... is 2.00"). Golden questions and citations both refer to sections. A fixed-size
window ignores all of that. It cuts the Generative AI policy's "Unacceptable Uses"
paragraph away from its list of examples, and it splits a clause from the heading that
says what the clause is about.

## Decision
`indexer/pipeline/chunk.py`:
1. Track a **heading path** while walking the lines: Roman-numeral chapters, then
   "N. Title" headings and HTML/Markdown headings, then short Title-Case headings. The
   path ("5. Academic Assessment > Academic Warning/Probation") becomes the chunk's
   `section` metadata, and it is also written as the first line of the chunk text, so
   the embedding knows which rule a bare clause belongs to.
2. Inside a section, split into **blocks** at clause numbers, list items, table rows
   and blank lines.
3. Pack whole blocks up to `CHUNK_SIZE` words, cut oversized blocks at sentence ends,
   and start each following chunk with the last `CHUNK_OVERLAP` words of the previous
   one. A chunk never crosses a section boundary.

Chunk ids are `<source-slug>-<position>`, so the same document gives the same ids on
every run. That makes the in-place re-index in ADR 0001 possible.

## Options considered
- **Fixed-size windows** (`chunk_fixed`, kept for the failure analysis): simplest, but
  it loses section boundaries; see FAILURE-ANALYSIS.md, stage B.
- **One chunk per clause:** precise, but many clauses are one line and carry no
  context ("5.5 If ... CGPA drops below 2.00, warning is issued").
- **Semantic chunking with embeddings:** costs an embedding call per sentence at index
  time, for a corpus whose structure is already explicit.

## Consequences
- Citations and recall@k can use human-readable sections that match the documents.
- Heading detection is heuristic. It occasionally promotes a table header ("Grade
  Points Grade Points") to a heading. That only makes a section name odd; no text is lost.
- Most sections are shorter than the chunk size, so overlap rarely applies and storage
  amplification stays low (PIPELINE.md has the numbers).
