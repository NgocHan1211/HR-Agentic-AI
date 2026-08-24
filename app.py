"""Interactive Streamlit demo for the deterministic payroll engine."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import pandas as pd
import streamlit as st

from payroll.anomaly_router import can_publish
from payroll.engine import run_payroll


st.set_page_config(page_title="AI Payroll Engine Demo", layout="wide")
st.title("AI Payroll Engine — Demo")
st.caption("Tính lương deterministic: công thức → input mapping → anomaly → publish gate.")


def demo_formula() -> dict:
    return {
        "formula_id": "F-DEMO-2025-01", "company_id": "DEMO", "status": "active", "calculation_basis": "monthly",
        "variables": [
            {"name": "basic_rate", "source": "rate_config", "field_code": "BASIC"},
            {"name": "attendance_allowance", "source": "rate_config", "field_code": "ATTENDANCE_ALLOWANCE"},
            {"name": "worked", "source": "attendance", "field_code": "total_working_days"},
            {"name": "standard", "source": "attendance", "field_code": "standard_working_days"},
            {"name": "ot", "source": "attendance", "field_code": "ot_day_shift_150_hours"},
        ],
        "rules": [
            {"output_field": "BASIC", "expression": "prorate(basic_rate, worked, standard)", "rounding": "round_down_1000"},
            {"output_field": "ATTENDANCE_ALLOWANCE", "expression": "prorate(attendance_allowance, worked, standard)"},
            {"output_field": "SALARY_OT_DAY_SHIFT_150", "expression": "BASIC / standard / 8 * ot * 1.5"},
            {"output_field": "SI_EE", "expression": "BASIC * 0.08", "section": "deductions"},
        ],
    }


with st.sidebar:
    st.header("Dữ liệu đầu vào")
    employee_id = st.text_input("Mã nhân viên", "DEMO-001")
    period = st.text_input("Kỳ lương", "2025-05")
    basic_rate = st.number_input("Lương cơ bản", min_value=0, value=4_730_000, step=100_000)
    allowance = st.number_input("Phụ cấp chuyên cần", min_value=0, value=550_000, step=50_000)
    worked = st.number_input("Ngày công thực tế", min_value=0.0, value=22.0, step=0.5)
    standard = st.number_input("Ngày công chuẩn", min_value=1.0, value=26.0, step=0.5)
    ot = st.number_input("Giờ OT 150%", min_value=0.0, value=8.0, step=1.0)
    previous_net = st.number_input("Net kỳ trước (0 = bỏ qua)", min_value=0, value=0, step=100_000)
    max_ot = st.number_input("Ngưỡng OT cảnh báo", min_value=0.0, value=200.0, step=1.0)

employee = {"employee_id": employee_id, "company_id": "DEMO", "employee_type": "official"}
attendance = {"period": period, "total_working_days": worked, "standard_working_days": standard, "ot_day_shift_150_hours": ot}
company = {"company_id": "DEMO", "anomaly_threshold_percent": 20, "minimum_wage": 3_500_000, "max_ot_hours": max_ot,
           "rate_config": [{"field_code": "BASIC", "employee_type": "official", "value": basic_rate},
                           {"field_code": "ATTENDANCE_ALLOWANCE", "employee_type": "*", "value": allowance}]}
history = [{"net_salary": previous_net}] if previous_net else []

try:
    result = run_payroll(employee, attendance, company, demo_formula(), history=history)
except (ValueError, ZeroDivisionError) as exc:
    st.error(f"Không thể tính lương: {exc}")
    st.stop()

metrics = st.columns(3)
metrics[0].metric("Gross salary", f"{result.gross_salary:,.0f} VND")
metrics[1].metric("Deductions", f"{sum(item.amount for item in result.deductions):,.0f} VND")
metrics[2].metric("Net salary", f"{result.net_salary:,.0f} VND")

st.subheader("Chi tiết kết quả")
rows = ([{"Nhóm": "Thu nhập", "Mã khoản": item.field_code, "Số tiền (VND)": item.amount} for item in result.line_items]
        + [{"Nhóm": "Khấu trừ", "Mã khoản": item.field_code, "Số tiền (VND)": item.amount} for item in result.deductions])
st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

st.subheader("Anomaly & publish gate")
if result.anomaly_flags:
    st.error("Có anomaly chưa xử lý — hệ thống chặn publish payslip.")
    st.dataframe(pd.DataFrame([flag.to_dict() for flag in result.anomaly_flags]), hide_index=True, use_container_width=True)
else:
    st.success("Không có anomaly — kết quả đủ điều kiện publish.")
st.write("Trạng thái publish:", "✅ Được phép" if can_publish(result) else "⛔ Bị chặn")

with st.expander("FormulaSpec đang chạy"):
    st.json(demo_formula())
with st.expander("Input snapshot (audit)"):
    st.json(result.input_snapshot)
