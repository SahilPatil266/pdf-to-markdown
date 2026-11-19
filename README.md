# pdf-to-markdown

Deterministic PDF text to Markdown converter (no AI, same input always gives the same output). Images are ignored.

Handles headings, paragraphs, bold/italic, bullet and numbered lists, ruled tables, borderless tables (invoices, statements), key/value rows, address blocks, code blocks, repeated headers/footers, and two-column pages.

```bash
pip install pymupdf
python pdf_to_markdown.py input.pdf                # writes input.md
python pdf_to_markdown.py input.pdf -o out.md --pages 1-5 --page-breaks
python pdf_to_markdown.py pdfs/ -o md/             # whole folder
```

Scanned (image-only) PDFs have no text layer and need OCR first.
