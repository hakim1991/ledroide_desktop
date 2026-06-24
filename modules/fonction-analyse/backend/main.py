import asyncio
import base64
from io import BytesIO

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from pydantic import BaseModel

from ledroide_sdk import Module

module = Module(__file__)
router = module.router


class FuncRequest(BaseModel):
    expression: str
    x_min: float = -10
    x_max: float = 10


@router.post("/analyze")
async def analyze(req: FuncRequest):
    def _q():
        from sympy import symbols, sympify, diff, solve, latex, S
        from sympy.calculus.util import continuous_domain
        from sympy.core.sympify import SympifyError

        try:
            x = symbols("x")
            f = sympify(req.expression)
            deriv = diff(f, x)
            deriv2 = diff(deriv, x)

            try:
                zeros = [str(z) for z in solve(f, x)[:20]]
            except Exception:
                zeros = ["Non calculable"]

            try:
                extrema = [str(e) for e in solve(deriv, x)[:20]]
            except Exception:
                extrema = ["Non calculable"]

            try:
                domain = str(continuous_domain(f, x, S.Reals))
            except Exception:
                domain = "Non calculable"

            return {
                "derivative": str(deriv),
                "derivative2": str(deriv2),
                "zeros": zeros,
                "extrema": extrema,
                "domain": domain,
                "expression_latex": latex(f),
            }
        except (SympifyError, Exception) as e:
            return {"error": f"Expression invalide : {e}", "code": "PARSE_ERROR"}

    return await asyncio.to_thread(_q)


@router.post("/plot")
async def plot(req: FuncRequest):
    def _q():
        from sympy import symbols, sympify, lambdify
        from sympy.core.sympify import SympifyError

        try:
            x_sym = symbols("x")
            f_sym = sympify(req.expression)
            f_num = lambdify(x_sym, f_sym, "numpy")

            x_vals = np.linspace(req.x_min, req.x_max, 600)
            y_vals = f_num(x_vals)

            # Scalaire → broadcast
            if np.isscalar(y_vals):
                y_vals = np.full_like(x_vals, float(y_vals))

            y_vals = np.where(np.isfinite(y_vals), y_vals, np.nan)

            plt.style.use("dark_background")
            fig, ax = plt.subplots(figsize=(9, 5))
            fig.patch.set_facecolor("#18181b")
            ax.set_facecolor("#18181b")

            ax.plot(x_vals, y_vals, color="#6366f1", linewidth=2)
            ax.axhline(0, color="#52525b", linewidth=0.8)
            ax.axvline(0, color="#52525b", linewidth=0.8)
            ax.grid(color="#27272a", linestyle="--", linewidth=0.5)
            ax.set_xlabel("x", color="#a1a1aa")
            ax.set_ylabel("f(x)", color="#a1a1aa")
            ax.set_title(f"f(x) = {req.expression}", color="#e2e2e8", fontsize=12)
            ax.tick_params(colors="#a1a1aa")
            for spine in ax.spines.values():
                spine.set_edgecolor("#3f3f46")

            plt.tight_layout()
            buf = BytesIO()
            plt.savefig(buf, format="png", bbox_inches="tight", facecolor="#18181b")
            buf.seek(0)
            img_b64 = base64.b64encode(buf.read()).decode()
            plt.close(fig)
            return {"image": img_b64}

        except (SympifyError, Exception) as e:
            return {"error": f"Impossible de tracer : {e}", "code": "PLOT_ERROR"}

    return await asyncio.to_thread(_q)
