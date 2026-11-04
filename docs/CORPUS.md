# Corpus

Source: FAST-NUCES public website (nu.edu.pk), public pages only · Documents: 9 (7 PDF,
2 HTML) · Pages: 279 PDF pages plus two HTML pages · Checked: 3–4 November 2026

None of these documents is committed. `corpus/MANIFEST.md` lists every URL with its size
and SHA-256, and `corpus/fetch.py` downloads them. Inside the cluster, the indexer Job's
`fetch-corpus` initContainer runs the same script into the `corpus-data` PVC, then
re-hashes the files against the committed manifest (see `docs/PIPELINE.md`). The CI
pipeline indexes `eval/fixtures/` instead, which are documents written for this project.

| File (as stored in `source` metadata) | Pages | Text layer | Version metadata |
| --- | ---: | --- | --- |
| `academic-rules-handbook-jun-2026.pdf` | 58 | yes; 5 near-empty cover/divider pages | `2026-06`, superseded `false` |
| `undergraduate-academic-rules-aug-2023.pdf` | 26 | **no**: 24 of 26 pages are scanned images | `2023-08`, superseded `true` |
| `prospectus-2026-academic-rules-and-regulations.pdf` | 5 | yes, two-column, letter-spaced | `2026`, superseded `false` |
| `student-handbook-jun-2026.pdf` | 64 | yes | `2026-06`, superseded `false` |
| `annual-report-2019-20.pdf` | 33 | yes | `2019-20`, superseded `false` |
| `annual-report-2023-24.pdf` | 85 | yes | `2023-24`, superseded `false` |
| `faculty-entrepreneurship-and-technical-start-ups-policy.pdf` | 8 | **none**: image-only | `2022-05`, superseded `false` |
| `fee-structure-page.html` | – | HTML, 3 tables | `2026-27`, superseded `false` |
| `grading-page.html` | – | HTML, 5 tables | `2026`, superseded `false` |

## The seven properties

| # | Required property | Document(s) that provide it | Evidence or notes |
| --- | --- | --- | --- |
| 1 | At least 6 documents, 40 pages | All nine above | 9 documents, 279 PDF pages (pypdf page count), plus two HTML pages. |
| 2 | Two versions of one policy | `undergraduate-academic-rules-aug-2023.pdf` (old) and `academic-rules-handbook-jun-2026.pdf` (current). The prospectus restates the 2026 rules with different clause numbers. | The 2026 handbook supersedes the 2023 rules. `corpus/sources.yaml` sets `doc_version: "2023-08", superseded: true` on the old file, and every chunk carries both fields. The two versions genuinely disagree. **Withdrawal deadline (4.25):** 2023 says "the last day of classes", 2026 says "the last day of the makeup week". **Summer semester (6.1–6.4):** 2023 has an "optional semester of 8 to 9 weeks" that allows new courses in three exceptions; 2026 has an "8-week semester" for four categories of student, with "only a repeat course". **Course load (4.3):** 2023 says students "can register for more courses provided they are not getting ahead of their batch"; 2026 says "a final year student may register in one additional course". **Failed course (5.8):** 2023 says repeat it "whenever that course is offered again"; 2026 says it is "mandatory in the subsequent semester". Golden questions v01–v04 test these. |
| 3 | Scanned, image-only PDF | `faculty-entrepreneurship-and-technical-start-ups-policy.pdf` (all 8 pages), and **also** `undergraduate-academic-rules-aug-2023.pdf` (24 of 26 pages) | `pdffonts` prints an empty font table (header only) for the entrepreneurship policy. pypdf finds 0 characters on all 8 pages and no `/Font` resource on any page. For the 2023 rules, `pdffonts` lists only the cover's three fonts (Calibri, Calibri-Bold, LEMONMILK), and pypdf finds 212 characters across 26 pages. So OCR is not optional: without it the old half of the version pair is empty, and the version test cannot even be posed. Tesseract 5 (in the indexer image) reads both scans; see PIPELINE.md for quality. |
| 4 | A table with real information | `fee-structure-page.html`, `grading-page.html`, `annual-report-2023-24.pdf` | Fee page: tuition per credit hour (Rs. 12,000), 11 miscellaneous fees, and the refund timeline (100% / 80% / 60% / 50% / none by day 10/15/20/30/31+). Grading page: three letter-grade scales and the warning thresholds by program (BS 2.0, MS 2.5, PhD 3.0). The annual report's faculty-by-campus and applications tables extract as runs of bare numbers (`Total 42 37 128 58 325 115 30 735`), which is the "jumbled columns" failure mode A. |
| 5 | Long definitions | `student-handbook-jun-2026.pdf`, section XII "Policy on Generative AI Based Tools" (pp. 37–49) | Twelve numbered sub-sections. "5. Unacceptable Uses of Generative AI Tools" is one ~350-word paragraph followed by a six-item list, and the list only makes sense with the paragraph. A 100-token fixed window cuts it mid-sentence (see FAILURE-ANALYSIS.md, stage B). |
| 6 | A deliberately absent topic | **Hostel rules**: we left out the prospectus's *Transport and Hostel Facilities* PDF | Hostels are a realistic question for students from other cities. The corpus mentions them exactly once (Student Handbook X: "In-Campus Hostel Facility is exclusively provided at Chiniot-Faisalabad and Peshawar Campuses"), so retrieval will find that passage and look relevant. The system must still decline questions about fees, curfew or allocation (golden a01–a03, i02), because that sentence does not answer them. |
| 7 | Exact-term content | All rules documents and the fee page | Clause numbers (1.21 attendance, 3.28 closure of admission, 4.25 withdrawal deadline, 5.4 warning threshold, 6.6 summer repeats); CGPA thresholds (2.00 / 2.5 / 3.0); SGPA 4.00 for the Rector's List; fees (Rs. 12,000 per credit hour, Rs. 500 rechecking, Rs. 5,000 campus transfer). The same rule also carries different numbers in different documents: attendance is clause 1.21 in the handbook but clause 1 in the prospectus. Golden questions e01–e05. |

## Traps we found while reading (not required, but they shape the pipeline)

- **The handbook prints every page's text twice.** pypdf returns each clause twice on
  most pages of `academic-rules-handbook-jun-2026.pdf`, so the clean stage removes the
  repeated lines. Without that, recall@k looks fine but the top-k is half duplicates.
- **Broken word spacing:** "attend ance", "s emester", "2. 00". The 2026 handbook
  splits words; the prospectus letter-spaces them ("t u t o r i a l s").
  An exact-term query for "2.00" will not match "2. 00".
- **The prospectus is two-column.** Text extraction interleaves the columns, so clause
  11 follows clause 6 in the extracted text.
- **Version drift on the site itself:** the Downloads page says "Prospectus 2025" while
  the prospectus page says 2026. We record the version the document itself prints.
