from dataclasses import dataclass
from typing import Optional


@dataclass
class Item:
    id: Optional[int]
    item_type: str
    name: str
    set_code: Optional[str]
    card_number: Optional[str]
    language: str
    condition: Optional[str]
    foil: bool
    variant: Optional[str]
    graded: bool
    grading_company: Optional[str]
    grade: Optional[str]
    cert_number: Optional[str]
    purchase_price: float
    purchase_date: str
    purchase_currency: str
    purchase_source: Optional[str]
    notes: Optional[str]

    @classmethod
    def from_row(cls, row) -> "Item":
        return cls(
            id=row["id"],
            item_type=row["item_type"],
            name=row["name"],
            set_code=row["set_code"],
            card_number=row["card_number"],
            language=row["language"] or "EN",
            condition=row["condition"],
            foil=bool(row["foil"]),
            variant=row["variant"],
            graded=bool(row["graded"]),
            grading_company=row["grading_company"],
            grade=row["grade"],
            cert_number=row["cert_number"],
            purchase_price=row["purchase_price"],
            purchase_date=row["purchase_date"],
            purchase_currency=row["purchase_currency"] or "EUR",
            purchase_source=row["purchase_source"],
            notes=row["notes"],
        )


@dataclass
class PriceSnapshot:
    id: Optional[int]
    item_id: int
    source: str
    price_type: str
    price: float
    currency: str
    url: Optional[str]
    fetched_at: str


@dataclass
class Receipt:
    id: Optional[int]
    item_id: int
    filename: str
    file_type: Optional[str]
    description: Optional[str]
    added_at: str
