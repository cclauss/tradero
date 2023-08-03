import inspect
import sys
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone


def get_strategies():
    available_strats = {
        i.slug: i
        for j, i in inspect.getmembers(sys.modules[__name__], inspect.isclass)
        if getattr(i, "slug", None)
    }
    return available_strats


class TradingStrategy:
    bot = None
    slug = None

    def evaluate_buy(self):
        raise NotImplementedError

    def evaluate_sell(self):
        raise NotImplementedError

    def evaluate_jump(self):
        raise NotImplementedError

    @property
    def time_safeguard(self):
        return (timezone.now() - self.bot.symbol.last_updated).seconds > 60


class ACMadness(TradingStrategy):
    """
    ACMAdness Trading Strategy

    (requires the STP indicator)
    """

    slug = "acmadness"

    def __init__(self, bot, symbol=None, **kwargs):
        self.bot = bot
        self.symbol = symbol or self.bot.symbol
        self.ac = Decimal(self.bot.symbol.others["stp"]["next_n_sum"])
        self.microgain = Decimal(kwargs.get("microgain", "0.3"))
        self.ac_threshold = self.microgain * Decimal(
            kwargs.get("ac_factor", 3)
        )
        self.keep_going = bool(int(kwargs.get("keep_going", "0")))
        self.outlier_protection = bool(int(kwargs.get("ol_prot", "1")))
        self.max_var_protection = Decimal(kwargs.get("max_var_prot", "0"))
        self.ac_adjusted = Decimal(kwargs.get("ac_adjusted", "1"))

    def evaluate_buy(self):
        if self.time_safeguard:
            return (
                False,
                "Time Safeguard - waiting for next turn...",
            )
        if (
            self.outlier_protection
            and self.bot.symbol.others["outliers"]["o1"]
        ):
            return (
                False,
                "Outlier Protection - waiting for next turn...",
            )
        if (
            self.max_var_protection > 0
            and self.bot.symbol.last_variation > self.max_var_protection
        ):
            return (
                False,
                f"Max Var Protection ({self.bot.symbol.last_variation:.3f} > "
                f"{self.max_var_protection:.3f}) - waiting for next turn...",
            )
        if self.get_ac() > self.ac_threshold:
            return True, None
        return (
            False,
            f"Current AC {'(adj) ' if self.ac_adjusted else '' }"
            f"({self.get_ac():.3f}) is below threshold "
            f"({self.ac_threshold:.3f})",
        )

    def evaluate_sell(self):
        selling_threshold = Decimal(self.bot.price_buying) * Decimal(
            1 + (self.microgain / 100)
        )
        if self.bot.price_current > selling_threshold:
            if self.keep_going:  # and self.cg:
                should_buy, message = self.evaluate_buy()
                if should_buy:
                    return (
                        False,
                        "Kept goin' due to current AC",
                    )
            return True, None
        return (
            False,
            f"Current price ({self.bot.price_current:.8f}) is below threshold "
            f"({selling_threshold:.8f})",
        )

    def evaluate_jump(self):
        if self.time_safeguard:
            return False, None
        symbols_with_siblings = list(
            self.bot.user.bots.filter(
                Q(
                    timestamp_buying__gt=timezone.now()
                    - timezone.timedelta(hours=1)
                )
                | Q(
                    timestamp_start__gt=timezone.now()
                    - timezone.timedelta(hours=1)
                ),
                status__gte=self.bot.Status.INACTIVE,
                strategy=self.bot.strategy,
                strategy_params=self.bot.strategy_params,
            ).values_list("symbol", flat=True)
        )
        symbols = self.bot.symbol._meta.concrete_model.objects.top_symbols()
        symbols = sorted(
            symbols, key=lambda s: s.others["stp"]["next_n_sum"], reverse=True
        )
        symbols_blacklist = self.bot.jumpy_blacklist.split(",")
        for symbol in symbols:
            symbol_ac = Decimal(symbol.others["stp"]["next_n_sum"])
            symbol_ac = (
                symbol_ac * symbol.model_score
                if self.ac_adjusted
                else symbol_ac
            )
            if (
                symbol_ac > self.ac_threshold
                and symbol.pk not in symbols_with_siblings
                and symbol.symbol not in symbols_blacklist
            ):
                if self.outlier_protection:
                    if symbol.others["outliers"]["o1"]:
                        continue
                return True, symbol
        return False, None

    def get_ac(self):
        if self.ac_adjusted:
            return self.ac * self.bot.symbol.model_score
        else:
            return self.ac


class CatchTheWave(TradingStrategy):
    """
    Catch The Wave Trading Strategy

    (requires the SCG indicator)
    """

    slug = "catchthewave"

    def __init__(self, bot, symbol=None, **kwargs):
        self.bot = bot
        self.symbol = symbol or self.bot.symbol
        self.scg = self.symbol.others["scg"]
        self.cg = self.scg["current_good"]
        self.diff_sm = self.scg["line_diff_sm"]  # Short - Middle tendency
        self.s_var = self.scg["line_s_var"]  # Var of the Short tendency
        #
        self.sell_on_maxima = bool(int(kwargs.get("sell_on_maxima", "1")))
        self.onset_periods = int(kwargs.get("onset_periods", "2"))
        self.maxima_tol = Decimal(kwargs.get("maxima_tol", "0.1"))
        self.sell_safeguard = Decimal(kwargs.get("sell_safeguard", "0.3"))

    def evaluate_buy(self):
        if self.cg and self.is_on_wave_onset():
            return True, None
        return (False, "Symbol is not in CG and ascending...")

    def evaluate_sell(self):
        if self.bot.price_current > self.get_min_selling_threshold():
            if self.sell_on_maxima and self.is_local_maxima():
                return True, None
            if self.is_on_wave():
                return (False, "Kept goin' due to still being in the wave")
            return True, None
        return (
            False,
            f"Current price ({self.bot.price_current:.8f}) is below min. "
            f"selling threshold ({self.get_min_selling_threshold():.8f})",
        )

    def evaluate_jump(self):
        symbols_with_siblings = self.get_symbols_with_siblings()
        symbols = self.bot.symbol._meta.concrete_model.objects.top_symbols()
        symbols = sorted(
            symbols,
            key=lambda s: s.others["scg"]["scg_index"],
            reverse=True,
        )
        symbols_blacklist = self.bot.jumpy_blacklist.split(",")
        for symbol in symbols:
            strat_in_symbol = self.bot.get_strategy(symbol)
            should_buy, _ = strat_in_symbol.evaluate_buy()
            if (
                should_buy
                and symbol.pk not in symbols_with_siblings
                and symbol.symbol not in symbols_blacklist
            ):
                return True, symbol
        return False, None

    def get_symbols_with_siblings(self):
        return self.bot.user.bots.filter(
            status__gte=self.bot.Status.INACTIVE,
            strategy=self.bot.strategy,
            strategy_params=self.bot.strategy_params,
        ).values_list("symbol", flat=True)

    def all_positive(self, ts):
        return all([t > 0 for t in ts])

    def is_local_maxima(self):
        return self.s_var[-1] * 100 < -self.maxima_tol

    def is_on_wave(self):
        return self.diff_sm[-1] > 0

    def is_on_wave_onset(self):
        return self.all_positive(self.s_var[-self.onset_periods :])

    def get_min_selling_threshold(self):
        return self.bot.price_buying * (1 + self.sell_safeguard / 100)
