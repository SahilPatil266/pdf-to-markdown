# pdf-to-markdown

Deterministic PDF text to Markdown converter. It uses no AI or ML (PyMuPDF's optional layout model is bypassed), so the same PDF gives the same Markdown on any machine. Images are ignored. Typically well under a second per document.

Format-agnostic: every rule is about layout (font size, weight, alignment, gaps, drawn lines), not about a document type. Tested on invoices, bank statements, forms, CVs, two-column papers, reports and a 33-page font manual.

Handles headings, paragraphs, bold/italic, bullet and numbered lists, ruled tables, borderless tables, tables continued across pages, key/value rows, checkboxes, address blocks, code blocks, repeated headers/footers and page numbers, and two-column pages.

```bash
pip install pymupdf
python pdf_to_markdown.py input.pdf                # writes input.md
python pdf_to_markdown.py input.pdf -o out.md --pages 1-5 --page-breaks
python pdf_to_markdown.py pdfs/ -o md/             # whole folder
```

Scanned (image-only) PDFs have no text layer and need OCR first.
