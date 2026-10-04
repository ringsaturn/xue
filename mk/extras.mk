# The format spec as a PDF (needs pandoc and a TeX Live with xetex).

.PHONY: format-pdf

format-pdf: ## Typeset docs/format.md as docs/format.pdf
	scripts/build_format_pdf.sh
