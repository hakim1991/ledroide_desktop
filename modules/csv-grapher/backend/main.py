import asyncio
import base64
from io import BytesIO

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from ledroide_sdk.http import UploadFile, File
from pydantic import BaseModel

from ledroide_sdk import Module

module = Module(__file__)
router = module.router


class ChartRequest(BaseModel):
    csv_b64: str
    x_col: str
    y_col: str
    chart_type: str = "bar"
    title: str = ""


@router.post("/analyze")
async def analyze(file: UploadFile = File(...)):
    content = await file.read()

    def _q():
        df = pd.read_csv(BytesIO(content))
        return {
            "columns": list(df.columns),
            "preview": df.head(5).to_dict(orient="records"),
            "row_count": len(df),
        }

    return await asyncio.to_thread(_q)


@router.post("/chart")
async def chart(req: ChartRequest):
    def _q():
        csv_bytes = base64.b64decode(req.csv_b64)
        df = pd.read_csv(BytesIO(csv_bytes))

        plt.style.use("dark_background")
        fig, ax = plt.subplots(figsize=(9, 5))
        fig.patch.set_facecolor("#18181b")
        ax.set_facecolor("#18181b")

        color = "#6366f1"

        if req.chart_type == "bar":
            ax.bar(df[req.x_col].astype(str), pd.to_numeric(df[req.y_col], errors="coerce"), color=color)
        elif req.chart_type == "line":
            ax.plot(df[req.x_col].astype(str), pd.to_numeric(df[req.y_col], errors="coerce"), color=color, marker="o", markersize=3)
        elif req.chart_type == "scatter":
            ax.scatter(pd.to_numeric(df[req.x_col], errors="coerce"), pd.to_numeric(df[req.y_col], errors="coerce"), color=color, alpha=0.7, s=20)
        elif req.chart_type == "pie":
            values = pd.to_numeric(df[req.y_col], errors="coerce").fillna(0)
            labels = df[req.x_col].astype(str)
            ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90,
                   colors=[color, "#818cf8", "#4f46e5", "#3730a3", "#a5b4fc"])

        if req.title:
            ax.set_title(req.title, color="#e2e2e8", fontsize=13)
        ax.set_xlabel(req.x_col, color="#a1a1aa")
        ax.set_ylabel(req.y_col if req.chart_type != "pie" else "", color="#a1a1aa")
        ax.tick_params(colors="#a1a1aa")
        for spine in ax.spines.values():
            spine.set_edgecolor("#27272a")
        ax.grid(color="#27272a", linestyle="--", linewidth=0.5, alpha=0.5)
        plt.tight_layout()

        buf = BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight", facecolor="#18181b")
        buf.seek(0)
        img_b64 = base64.b64encode(buf.read()).decode()
        plt.close(fig)
        return {"image": img_b64}

    return await asyncio.to_thread(_q)
