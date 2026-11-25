"""Explicit local bot identity and paper risk policy."""

from dataclasses import asdict, dataclass

from paper_quotes import positive_int, text


@dataclass(frozen=True)
class PaperLimits:
    max_buy_quantity: int
    max_buy_value: int
    max_buy_price: int
    max_daily_purchase_value: int
    max_open_exposure: int
    max_sell_quantity: int
    max_sell_value: int
    min_sell_price: int
    max_daily_loss: int

    def __post_init__(self):
        for name, value in asdict(self).items():
            positive_int(value, name)


@dataclass(frozen=True)
class BotPolicy:
    bot_id: str
    strategy_id: str
    limits: PaperLimits

    def __post_init__(self):
        for field in ("bot_id", "strategy_id"):
            value = getattr(self, field)
            if text(value, field) != value or len(value) > 128:
                raise ValueError(f"invalid {field}")
        if not isinstance(self.limits, PaperLimits):
            raise ValueError("verified paper limits are required")

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) != {"bot_id", "strategy_id", "limits"}:
            raise ValueError("policy requires bot_id, strategy_id and limits")
        try:
            return cls(data["bot_id"], data["strategy_id"], PaperLimits(**data["limits"]))
        except TypeError as error:
            raise ValueError("invalid or incomplete paper limits") from error

    def report(self, *, purchased, loss):
        limits = self.limits
        return {
            "شناسهٔ ربات": self.bot_id,
            "شناسهٔ راهبرد": self.strategy_id,
            "سقف تعداد خرید": limits.max_buy_quantity,
            "سقف ارزش سفارش خرید": limits.max_buy_value,
            "سقف قیمت واحد خرید": limits.max_buy_price,
            "سقف ارزش خرید روزانه": limits.max_daily_purchase_value,
            "سقف بهای خرید موقعیت‌های باز": limits.max_open_exposure,
            "سقف تعداد فروش": limits.max_sell_quantity,
            "سقف ارزش سفارش فروش": limits.max_sell_value,
            "کف قیمت واحد فروش": limits.min_sell_price,
            "سقف زیان تحقق‌یافتهٔ روزانه": limits.max_daily_loss,
            "ظرفیت باقیماندهٔ خرید امروز": max(0, limits.max_daily_purchase_value - purchased),
            "ظرفیت باقیماندهٔ زیان امروز": None if loss is None else max(0, limits.max_daily_loss - loss),
        }
