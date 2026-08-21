#!/bin/sh
# Render docs/format.md to docs/format.pdf with pandoc + XeLaTeX.
#
# Needs: pandoc, a TeX Live with xetex (Debian/Ubuntu: pandoc texlive-xetex
# texlive-fonts-recommended texlive-latex-recommended lmodern).
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
src=${1:-$root/docs/format.md}
out=${2:-$root/docs/format.pdf}

pandoc "$src" -o "$out" \
  --pdf-engine=xelatex \
  --lua-filter="$root/scripts/pandoc_table_widths.lua" \
  --shift-heading-level-by=-1 \
  --toc --toc-depth=2 --number-sections \
  --highlight-style=tango \
  -V documentclass=article \
  -V papersize=a4 \
  -V geometry:margin=2.5cm \
  -V fontsize=10pt \
  -V mainfont="Latin Modern Roman" \
  -V sansfont="Latin Modern Sans" \
  -V monofont="DejaVu Sans Mono" \
  -V monofontoptions="Scale=0.82" \
  -V colorlinks=true \
  -V linkcolor=RoyalBlue \
  -V urlcolor=RoyalBlue \
  -V toccolor=black \
  -V header-includes='\usepackage{etoolbox}' \
  -V header-includes='\AtBeginEnvironment{longtable}{\footnotesize}'
