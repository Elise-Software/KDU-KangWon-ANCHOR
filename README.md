# KNU-KangWon-ANCHOR

## Wonju public medical-data integration

Run from the repository root in PowerShell. The script detects UTF-8,
UTF-8-SIG, CP949, and EUC-KR inputs; it does not alter source files.

```powershell
& .\.venv\Scripts\python.exe .\scripts\integrate_wonju_public_data.py `
  --master .\data\wonju_medical_institutions.csv `
  --clinic '.\data\raw\공공데이터포털\강원특별자치도 원주시_의원 정보_20251125.csv' `
  --mental '.\data\raw\공공데이터포털\강원도 원주시_정신건강증진시설 및 요양병원 현황_20220813.csv' `
  --dental-lab '.\data\raw\공공데이터포털\강원도 원주시_치과기공소 현황_20230612.csv' `
  --aliases .\config\wonju_institution_aliases.csv `
  --output-dir .\data\integrated\wonju `
  --strict
```

The output directory contains the canonical institutions table, aliases,
provenance, coordinates, conflicts, manual-review items, and the integration
report. Coordinates are written only to the separate coordinate table.
