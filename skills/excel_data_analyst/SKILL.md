---
name: excel_data_analyst
title: 📈 Excel Data Analyst & Visualizer
description: Create Excel (.xlsx) spreadsheets, financial models, pivot summaries, automated data calculations, and export publication-ready charts using pandas, openpyxl, and matplotlib.
keywords: [excel, xlsx, spreadsheet, pandas, openpyxl, dataframe, data analysis, chart, plot, matplotlib, csv, data processing, اکسل, نمودار]
packages: [pandas, openpyxl, matplotlib]
---

# Excel Data Analyst & Visualizer Skill

Processes datasets, creates `.xlsx` files, and generates statistical charts.

## Core Guidelines & Best Practices

1. **Clean Spreadsheet Creation with pandas & openpyxl**:
   ```python
   import pandas as pd
   import matplotlib.pyplot as plt

   df = pd.DataFrame({
       "Month": ["Jan", "Feb", "Mar", "Apr", "May", "Jun"],
       "Revenue": [12000, 15400, 18900, 22100, 26500, 31200],
       "Expenses": [8000, 9200, 11000, 12500, 14000, 15800]
   })
   df["Profit"] = df["Revenue"] - df["Expenses"]
   df.to_excel("financial_summary.xlsx", index=False, engine="openpyxl")
   ```
2. **Chart Generation**:
   - Set modern styling, tight layout, and DPI=300 for crisp images.
   ```python
   plt.style.use("seaborn-v0_8-whitegrid" if "seaborn-v0_8-whitegrid" in plt.style.available else "default")
   fig, ax = plt.subplots(figsize=(8, 4.5), dpi=300)
   ax.plot(df["Month"], df["Revenue"], marker='o', label="Revenue", color="#3B82F6", linewidth=2)
   ax.plot(df["Month"], df["Expenses"], marker='s', label="Expenses", color="#EF4444", linewidth=2)
   ax.set_title("Monthly Financial Trajectory", fontsize=14, fontweight="bold")
   ax.legend()
   plt.tight_layout()
   plt.savefig("chart_revenue.png")
   plt.close()
   ```
