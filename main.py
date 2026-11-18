
import argparse
import csv
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

CONTRACT_HEADERS = {
    "contract_id", "contractor", "amount", "signed_at"
}
WORK_HEADERS = {
    "work_id", "contract_id", "title", "price", "status",
    "completed_at", "cancelled_at"
}
PAYMENT_HEADERS = {
    "payment_id", "contract_id", "work_id", "purpose",
    "amount", "status", "created_at", "paid_at", "refunded_at"
}


class ReportError(Exception):
    """Ошибка исходных данных или расчёта."""


def parse_money(value, field_name):
    """Преобразует сумму в Decimal с точностью до копейки."""
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ReportError(f"Некорректная сумма в поле {field_name}: {value}")

    if not amount.is_finite() or amount < ZERO:
        raise ReportError(f"Недопустимая сумма в поле {field_name}: {value}")

    if amount != amount.quantize(CENT):
        raise ReportError(
            f"Сумма должна быть указана до копейки: {field_name}={value}"
        )

    return amount


def parse_date(value, field_name, required=False):
    value = (value or "").strip()

    if not value:
        if required:
            raise ReportError(f"Не заполнена обязательная дата: {field_name}")
        return None

    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ReportError(f"Некорректная дата {field_name}: {value}")


def load_csv(filename, required_headers):
    path = DATA_DIR / filename

    if not path.exists():
        raise ReportError(f"Файл не найден: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        if not reader.fieldnames:
            raise ReportError(f"Файл пустой или нет заголовков: {filename}")

        missing = required_headers - set(reader.fieldnames)

        if missing:
            raise ReportError(
                f"В {filename} отсутствуют колонки: {', '.join(sorted(missing))}"
            )

        rows = []

        for line_number, row in enumerate(reader, start=2):
            if None in row:
                raise ReportError(
                    f"Лишние колонки в {filename}, строка {line_number}"
                )

            rows.append(row)

    return rows


def unique_index(rows, field_name, filename):
    result = {}

    for row in rows:
        key = row[field_name].strip()

        if not key:
            raise ReportError(
                f"Пустое значение {field_name} в файле {filename}"
            )

        if key in result:
            raise ReportError(
                f"Повторяется {field_name}={key} в файле {filename}"
            )

        result[key] = row

    return result


def money_to_cents(amount):
    return int(amount * 100)


def cents_to_money(cents):
    return (Decimal(cents) / 100).quantize(CENT)


def money_text(amount):
    return f"{amount:.2f}"


def get_work_status_as_of(work, report_date):
    """
    Восстанавливает статус работы на дату отчёта,
    используя даты приёмки и отмены.
    """
    status = work["status"]

    if status == "cancelled":
        cancelled_at = work["cancelled_at"]

        if cancelled_at <= report_date:
            return "cancelled"

        return "planned"

    if status == "done":
        completed_at = work["completed_at"]

        if completed_at <= report_date:
            return "done"

        return "planned"

    return "planned"


def get_payment_amount_as_of(payment, report_date):
    """
    Возвращает фактически оставшуюся у подрядчика сумму
    по платежу на дату отчёта.
    """
    status = payment["status"]
    paid_at = payment["paid_at"]
    refunded_at = payment["refunded_at"]
    amount = payment["amount"]

    if status == "queued":
        return ZERO

    if paid_at is None or paid_at > report_date:
        return ZERO

    if status == "paid":
        return amount

    if status == "refunded":
        if refunded_at is None:
            raise ReportError(
                f"У возвращённого платежа {payment['payment_id']} "
                f"не заполнена дата возврата"
            )

        if refunded_at <= paid_at:
            raise ReportError(
                f"Возврат платежа {payment['payment_id']} "
                f"не может быть раньше или в день оплаты"
            )

        if refunded_at <= report_date:
            return ZERO

        return amount

    raise ReportError(
        f"Неизвестный статус платежа {payment['payment_id']}: {status}"
    )


def allocate_proportionally(total, works):
    """
    Распределяет сумму между работами пропорционально их цене.

    Все вычисления выполняются в целых копейках.
    Остаточные копейки распределяются методом наибольших остатков.
    """
    if total == ZERO:
        return {work["work_id"]: ZERO for work in works}

    total_cents = money_to_cents(total)
    total_price_cents = sum(
        money_to_cents(work["price"]) for work in works
    )

    if total_price_cents == 0:
        return {}

    allocations = {}
    remainders = {}
    allocated_cents = 0

    for work in works:
        price_cents = money_to_cents(work["price"])

        numerator = total_cents * price_cents
        base = numerator // total_price_cents
        remainder = numerator % total_price_cents

        allocations[work["work_id"]] = base
        remainders[work["work_id"]] = remainder
        allocated_cents += base

    cents_left = total_cents - allocated_cents

    ordered_ids = sorted(
        remainders,
        key=lambda work_id: (-remainders[work_id], work_id)
    )

    for work_id in ordered_ids[:cents_left]:
        allocations[work_id] += 1

    return {
        work_id: cents_to_money(cents)
        for work_id, cents in allocations.items()
    }


def build_report(report_date):
    contracts_raw = load_csv("contracts.csv", CONTRACT_HEADERS)
    works_raw = load_csv("works.csv", WORK_HEADERS)
    payments_raw = load_csv("payments.csv", PAYMENT_HEADERS)

    contracts = unique_index(
        contracts_raw, "contract_id", "contracts.csv"
    )
    works = unique_index(works_raw, "work_id", "works.csv")
    payments = unique_index(
        payments_raw, "payment_id", "payments.csv"
    )

    warnings = []

    # Подготовка договоров.
    for contract in contracts.values():
        contract["amount"] = parse_money(
            contract["amount"], "contracts.amount"
        )
        contract["signed_at"] = parse_date(
            contract["signed_at"], "contracts.signed_at", True
        )

    # Подготовка работ.
    for work in works.values():
        contract_id = work["contract_id"].strip()

        if contract_id not in contracts:
            raise ReportError(
                f"Работа {work['work_id']} ссылается "
                f"на неизвестный договор {contract_id}"
            )

        work["price"] = parse_money(
            work["price"], "works.price"
        )

        if work["status"] not in {"planned", "done", "cancelled"}:
            raise ReportError(
                f"Неизвестный статус работы {work['work_id']}: "
                f"{work['status']}"
            )

        work["completed_at"] = parse_date(
            work["completed_at"], "works.completed_at"
        )
        work["cancelled_at"] = parse_date(
            work["cancelled_at"], "works.cancelled_at"
        )

        if work["status"] == "done" and work["completed_at"] is None:
            raise ReportError(
                f"У выполненной работы {work['work_id']} "
                f"не заполнена дата приёмки"
            )

        if work["status"] == "cancelled" and work["cancelled_at"] is None:
            raise ReportError(
                f"У отменённой работы {work['work_id']} "
                f"не заполнена дата отмены"
            )

        work["status_as_of"] = get_work_status_as_of(
            work, report_date
        )

    # Подготовка платежей.
    for payment in payments.values():
        contract_id = payment["contract_id"].strip()

        if contract_id not in contracts:
            raise ReportError(
                f"Платёж {payment['payment_id']} ссылается "
                f"на неизвестный договор {contract_id}"
            )

        payment["amount"] = parse_money(
            payment["amount"], "payments.amount"
        )

        if payment["amount"] == ZERO:
            warnings.append(
                f"Платёж {payment['payment_id']} имеет нулевую сумму"
            )

        if payment["status"] not in {"paid", "queued", "refunded"}:
            raise ReportError(
                f"Неизвестный статус платежа {payment['payment_id']}: "
                f"{payment['status']}"
            )

        payment["created_at"] = parse_date(
            payment["created_at"], "payments.created_at", True
        )
        payment["paid_at"] = parse_date(
            payment["paid_at"], "payments.paid_at"
        )
        payment["refunded_at"] = parse_date(
            payment["refunded_at"], "payments.refunded_at"
        )

        if payment["status"] in {"paid", "refunded"}:
            if payment["paid_at"] is None:
                raise ReportError(
                    f"У платежа {payment['payment_id']} "
                    f"нет даты фактической оплаты"
                )

        if payment["status"] == "refunded":
            if payment["refunded_at"] is None:
                raise ReportError(
                    f"У возвращённого платежа "
                    f"{payment['payment_id']} нет даты возврата"
                )

        work_id = (payment["work_id"] or "").strip()

        if work_id:
            if work_id not in works:
                raise ReportError(
                    f"Платёж {payment['payment_id']} "
                    f"ссылается на неизвестную работу {work_id}"
                )

            if works[work_id]["contract_id"] != contract_id:
                raise ReportError(
                    f"Работа {work_id} и платёж "
                    f"{payment['payment_id']} относятся к разным договорам"
                )

        payment["work_id"] = work_id or None
        payment["net_amount"] = get_payment_amount_as_of(
            payment, report_date
        )

    # Группировка работ по договорам.
    works_by_contract = defaultdict(list)

    for work in works.values():
        works_by_contract[work["contract_id"]].append(work)

    # Суммы выплат по каждой работе.
    work_paid = {work_id: ZERO for work_id in works}

    # Общие платежи распределяем пропорционально.
    contract_unallocated = defaultdict(lambda: ZERO)
    contract_paid_total = defaultdict(lambda: ZERO)

    for payment in payments.values():
        amount = payment["net_amount"]
        contract_id = payment["contract_id"]

        contract_paid_total[contract_id] += amount

        if amount == ZERO:
            continue

        if payment["work_id"]:
            work = works[payment["work_id"]]

            if work["status_as_of"] == "cancelled":
                contract_unallocated[contract_id] += amount
                warnings.append(
                    f"Платёж {payment['payment_id']} привязан "
                    f"к отменённой работе {payment['work_id']}; "
                    f"сумма не распределена"
                )
            else:
                work_paid[payment["work_id"]] += amount

        else:
            active_works = [
                work
                for work in works_by_contract[contract_id]
                if work["status_as_of"] != "cancelled"
            ]

            allocations = allocate_proportionally(
                amount, active_works
            )

            if not allocations:
                contract_unallocated[contract_id] += amount
                warnings.append(
                    f"Платёж {payment['payment_id']} "
                    f"не удалось распределить: нет действующих работ"
                )
            else:
                for work_id, allocated_amount in allocations.items():
                    work_paid[work_id] += allocated_amount

    # Формируем строки отчёта по работам.
    work_report = []

    for work in sorted(
        works.values(),
        key=lambda item: (item["contract_id"], item["work_id"])
    ):
        status = work["status_as_of"]
        price = work["price"]

        if status == "cancelled":
            paid = ZERO
            remaining = ZERO
            overpaid = ZERO
        else:
            paid = work_paid[work["work_id"]]
            remaining = max(ZERO, price - paid)
            overpaid = max(ZERO, paid - price)

        work_report.append({
            "contract_id": work["contract_id"],
            "contractor": contracts[work["contract_id"]]["contractor"],
            "work_id": work["work_id"],
            "title": work["title"],
            "status": status,
            "price": price,
            "paid": paid,
            "remaining": remaining,
            "overpaid": overpaid,
        })

    # Формируем итоги по договорам.
    contract_report = []

    for contract_id, contract in sorted(contracts.items()):
        contract_works = works_by_contract[contract_id]

        active_works = [
            work for work in contract_works
            if work["status_as_of"] != "cancelled"
        ]

        cancelled_works = [
            work for work in contract_works
            if work["status_as_of"] == "cancelled"
        ]

        active_price = sum(
            (work["price"] for work in active_works), ZERO
        )

        cancelled_price = sum(
            (work["price"] for work in cancelled_works), ZERO
        )

        total_work_price = active_price + cancelled_price

        allocated_paid = sum(
            (work_paid[work["work_id"]] for work in active_works), ZERO
        )

        unallocated_paid = contract_unallocated[contract_id]

        paid_total = contract_paid_total[contract_id]

        remaining = sum(
            (
                max(ZERO, work["price"] - work_paid[work["work_id"]])
                for work in active_works
            ),
            ZERO
        )

        contract_balance = contract["amount"] - paid_total
        contract_difference = contract["amount"] - active_price

        if contract_difference != ZERO:
            warnings.append(
                f"Договор {contract_id}: сумма договора отличается "
                f"от стоимости действующих работ на "
                f"{money_text(contract_difference)} руб."
            )

        contract_report.append({
            "contract_id": contract_id,
            "contractor": contract["contractor"],
            "contract_amount": contract["amount"],
            "total_work_price": total_work_price,
            "active_work_price": active_price,
            "cancelled_work_price": cancelled_price,
            "paid_total": paid_total,
            "allocated_paid": allocated_paid,
            "unallocated_paid": unallocated_paid,
            "work_remaining": remaining,
            "contract_balance": contract_balance,
            "contract_difference": contract_difference,
        })

    return work_report, contract_report, warnings


def save_csv(filename, rows, columns):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    path = OUTPUT_DIR / filename

    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=columns,
            delimiter=";"
        )

        writer.writeheader()

        for row in rows:
            formatted_row = {}

            for key in columns:
                value = row[key]

                if isinstance(value, Decimal):
                    formatted_row[key] = money_text(value)
                else:
                    formatted_row[key] = value

            writer.writerow(formatted_row)

    return path


def main():
    parser = argparse.ArgumentParser(
        description="Отчёт по авансам и оплатам договоров"
    )

    parser.add_argument(
        "--date",
        required=True,
        help="Дата отчёта в формате YYYY-MM-DD"
    )

    args = parser.parse_args()

    try:
        report_date = date.fromisoformat(args.date)

        work_report, contract_report, warnings = build_report(
            report_date
        )

        work_columns = [
            "contract_id", "contractor", "work_id", "title",
            "status", "price", "paid", "remaining", "overpaid"
        ]

        contract_columns = [
            "contract_id", "contractor", "contract_amount",
            "total_work_price", "active_work_price",
            "cancelled_work_price", "paid_total", "allocated_paid",
            "unallocated_paid", "work_remaining",
            "contract_balance", "contract_difference"
        ]

        work_path = save_csv(
            "work_report.csv", work_report, work_columns
        )

        contract_path = save_csv(
            "contract_report.csv", contract_report, contract_columns
        )

        print()
        print("=" * 55)
        print(f"ОТЧЁТ ПО ДОГОВОРАМ НА {report_date.strftime('%d.%m.%Y')}")
        print("=" * 55)

        for contract in contract_report:
            print()
            print(
                f"{contract['contract_id']} — "
                f"{contract['contractor']}"
            )
            print(
                f"Сумма договора: "
                f"{money_text(contract['contract_amount'])} руб."
            )
            print(
                f"Фактически выплачено: "
                f"{money_text(contract['paid_total'])} руб."
            )
            print(
                f"Остаток по работам: "
                f"{money_text(contract['work_remaining'])} руб."
            )
            print(
                f"Остаток по сумме договора: "
                f"{money_text(contract['contract_balance'])} руб."
            )

        print()
        print("Файлы отчёта созданы:")
        print(work_path)
        print(contract_path)

        if warnings:
            print()
            print("ПРЕДУПРЕЖДЕНИЯ:")

            for warning in warnings:
                print(f"- {warning}")

        print()
        print("Расчёт завершён успешно.")

    except (ReportError, ValueError) as error:
        print(f"ОШИБКА: {error}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
