# Put your CV here

Supported formats: `.pdf`, `.docx`, `.txt`, and legacy `.doc` when `antiword` or LibreOffice is installed.

Application automation (`python main.py --apply basic`) attaches your original CV as a **PDF**, so keep a
PDF version here. Files in this folder are only read, never modified. Tailored CVs (`--apply ai`) are
written to `data/generated_cvs/`.

- Multiple CVs are allowed; the application asks which one to use.
- Image-only (scanned) PDFs are detected and reported: they contain no extractable
  text, so convert them with OCR first or supply a DOCX/TXT version.
- Files in this folder are ignored by git (`.gitignore`) and never uploaded anywhere
  except, with your consent, a redacted text version to the configured AI provider.
