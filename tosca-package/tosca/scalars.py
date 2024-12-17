# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Expose TOSCA units as Python objects
Usage:

>>> from tosca import mb
>>> one_mb = 1 * mb
>>> one_mb
1.0 MB
>>> one_mb.value
1000000.0
>>> one_mb.as_unit
1.0
>>> one_mb.to_yaml()
'1.0 MB'
"""

import builtins
import math
from typing import (
    Any,
    Generic,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Dict,
    Union,
    cast,
    overload,
)
from typing_extensions import Literal, SupportsIndex, Self
from toscaparser.elements.scalarunit import (
    ScalarUnit_Size,
    ScalarUnit_Time,
    ScalarUnit_Frequency,
    ScalarUnit_Bitrate,
    parse_scalar_unit,
)


class _Scalar:
    __slots__ = ("unit", "value")  # unit to represent this value as
    SCALAR_UNIT_DICT: Dict[str, Any] = {}

    def __init__(self, value, unit: "_Unit"):
        self.value = float(value)
        self.unit = unit

    def __float__(self) -> float:
        return self.value

    def __int__(self) -> int:
        return int(self.value)

    def __bool__(self) -> bool:
        return bool(self.value)

    def __str__(self) -> str:
        return self.to_yaml()

    def __repr__(self) -> str:
        return f"{self.as_unit}*{self.unit}"

    @property
    def as_unit(self) -> float:
        return self.value / self.unit.value

    def to_yaml(self, dict_cls=dict) -> str:
        "Return this value and this type's TOSCA unit suffix, eg. 10 kB"
        val = self.as_unit
        as_int = math.floor(val)
        if val == as_int:
            val = as_int  # whole number, treat as int
        return f"{val} {self.unit}"

    def as_ref(self, options=None):
        return {"eval": dict(scalar=str(self))}

    def __mul__(self, other: Union[int, float, Self, "_Unit[Self]"]) -> Self:
        if isinstance(other, _Unit):
            if type(self) != other.scalar_type:
                raise TypeError(f"Wrong unit {other}, should be a {type(self)}")
            return self.__class__(self.value, other)
        return self.__class__(self.value * float(other), self.unit)

    def __add__(self, other: Union[int, float, Self]) -> Self:
        return self.__class__(self.value + float(other), self.unit)

    def __sub__(self, other: Union[int, float, Self]) -> Self:
        return self.__class__(self.value - float(other), self.unit)

    def __truediv__(self, other: Union[int, float, Self]) -> Self:
        return self.__class__(self.value / float(other), self.unit)

    def __floordiv__(self, other: Union[int, float, Self]) -> Self:
        return self.__class__(self.value // float(other), self.unit)

    def __mod__(self, other: Union[int, float, Self]) -> Self:
        return self.__class__(self.value % float(other), self.unit)

    def __rmul__(self, other: Union[int, float, Self]) -> Self:
        return self.__class__(self.value * float(other), self.unit)

    def __radd__(self, other: Union[int, float, Self]) -> Self:
        return self.__class__(self.value + float(other), self.unit)

    def __rsub__(self, other: Union[int, float, Self]) -> Self:
        return self.__class__(self.value - float(other), self.unit)

    def __rtruediv__(self, other: Union[int, float, Self]) -> Self:
        return self.__class__(self.value / float(other), self.unit)

    def __rfloordiv__(self, other: Union[int, float, Self]) -> Self:
        return self.__class__(self.value // float(other), self.unit)

    def __rmod__(self, other: Union[int, float, Self]) -> Self:
        return self.__class__(self.value % float(other), self.unit)

    def __trunc__(self) -> int:
        return math.trunc(self.value)

    def __ceil__(self) -> int:
        return math.ceil(self.value)

    def __floor__(self) -> int:
        return math.floor(self.value)

    @overload
    def __round__(self, ndigits: None = None, /) -> int: ...

    @overload
    def __round__(self, ndigits: SupportsIndex, /) -> float: ...

    def __round__(self, ndigits=None):
        if ndigits is None:
            return round(self.value)
        return round(self.value, ndigits)

    def __eq__(self, value: object, /) -> bool:
        eq = self.value == value
        if eq and isinstance(value, _Scalar) and self.tosca_name != value.tosca_name:  # type: ignore
            return False
        return eq

    def __ne__(self, value: object, /) -> bool:
        ne = self.value != value
        if not ne:
            if isinstance(value, _Scalar) and self.tosca_name != value.tosca_name:  # type: ignore
                return True  # equal values but different types
        return ne

    def __lt__(self, value: float, /) -> bool:
        ans = self.value < value
        if ans and isinstance(value, _Scalar) and self.tosca_name != value.tosca_name:  # type: ignore
            return False
        return ans

    def __le__(self, value: float, /) -> bool:
        ans = self.value <= value
        if ans and isinstance(value, _Scalar) and self.tosca_name != value.tosca_name:  # type: ignore
            return False
        return ans

    def __gt__(self, value: float, /) -> bool:
        ans = self.value > value
        if ans and isinstance(value, _Scalar) and self.tosca_name != value.tosca_name:  # type: ignore
            return False
        return ans

    def __ge__(self, value: float, /) -> bool:
        ans = self.value >= value
        if ans and isinstance(value, _Scalar) and self.tosca_name != value.tosca_name:  # type: ignore
            return False
        return ans

    def __neg__(self) -> float:
        return -self.value

    def __pos__(self) -> float:
        return self.value

    def __abs__(self) -> float:
        return abs(self.value)

    def __hash__(self) -> int:
        return hash(self.value)


_S = TypeVar("_S", bound="_Scalar")


class _Unit(Generic[_S]):
    def __init__(self, scalar_type: Type[_S], unit: str):
        self.unit = unit
        self.scalar_type = scalar_type
        self.value = scalar_type.SCALAR_UNIT_DICT[unit]

    def __rmul__(self, other: float) -> _S:
        return self.scalar_type(self.value * other, self)

    def __str__(self) -> str:
        return self.unit

    def as_int(
        self,
        s: _S,
        round: Union[Literal["round"], Literal["ceil"], Literal["floor"]] = "round",
    ) -> int:
        "Return the given scalar as an integer denominated by this unit, rounding up or down to the nearest integer."
        return cast(int, self._round(s, round))

    def as_float(self, s: _S, ndigits: int = 16) -> float:
        "Return the given scalar as an float denominated by this unit. If ndigits is given, round to that many digits."
        return cast(int, self._round(s, ndigits))

    def _round(
        self,
        s: _S,
        ndigits: Union[int, None, Literal["ceil"], Literal["floor"], Literal["round"]],
    ) -> Union[int, float]:
        # yes, this is a layering violation
        from tosca import global_state_mode, EvalData

        if ndigits == "round":
            ndigits = None

        if global_state_mode() == "runtime" or not isinstance(s, EvalData):
            return cast(Union[int, float], scalar_value(s, self.unit, ndigits))
        else:
            return cast(
                int,
                EvalData({
                    "eval": {
                        "scalar_value": str(s),
                        "unit": self.unit,
                        "round": ndigits,
                    }
                }),
            )


class Size(_Scalar):
    tosca_name = "scalar-unit.size"
    SCALAR_UNIT_DICT = ScalarUnit_Size.SCALAR_UNIT_DICT


class Frequency(_Scalar):
    tosca_name = "scalar-unit.frequency"
    SCALAR_UNIT_DICT = ScalarUnit_Frequency.SCALAR_UNIT_DICT


class Time(_Scalar):
    tosca_name = "scalar-unit.time"
    SCALAR_UNIT_DICT = ScalarUnit_Time.SCALAR_UNIT_DICT


class Bitrate(_Scalar):
    tosca_name = "scalar-unit.bitrate"
    SCALAR_UNIT_DICT = ScalarUnit_Bitrate.SCALAR_UNIT_DICT


B = _Unit(Size, "B")
b = B
kB = _Unit(Size, "kB")
kb = kB
KB = kB
KiB = _Unit(Size, "KiB")
kib = KiB
KIB = KiB
MB = _Unit(Size, "MB")
mb = MB
MiB = _Unit(Size, "MiB")
mib = MiB
MIB = MiB
GB = _Unit(Size, "GB")
gb = GB
GiB = _Unit(Size, "GiB")
gib = GiB
GIB = GiB
TB = _Unit(Size, "TB")
tb = TB
TiB = _Unit(Size, "TiB")
tib = TiB
TIB = TiB
d = _Unit(Time, "d")
D = d
h = _Unit(Time, "h")
H = h
m = _Unit(Time, "m")
M = m
s = _Unit(Time, "s")
S = s
ms = _Unit(Time, "ms")
MS = ms
us = _Unit(Time, "us")
US = us
ns = _Unit(Time, "ns")
NS = ns
Hz = _Unit(Frequency, "Hz")
hz = Hz
HZ = Hz
kHz = _Unit(Frequency, "kHz")
khz = kHz
KHZ = kHz
MHz = _Unit(Frequency, "MHz")
mhz = MHz
MHZ = MHz
GHz = _Unit(Frequency, "GHz")
ghz = GHz
GHZ = GHz
bps = _Unit(Bitrate, "bps")
BPS = bps
Kbps = _Unit(Bitrate, "Kbps")
kbps = Kbps
KBPS = Kbps
Kibps = _Unit(Bitrate, "Kibps")
kibps = Kibps
KIBPS = Kibps
Mbps = _Unit(Bitrate, "Mbps")
mbps = Mbps
MBPS = Mbps
Mibps = _Unit(Bitrate, "Mibps")
mibps = Mibps
MIBPS = Mibps
Gbps = _Unit(Bitrate, "Gbps")
gbps = Gbps
GBPS = Gbps
Gibps = _Unit(Bitrate, "Gibps")
gibps = Gibps
GIBPS = Gibps
Tbps = _Unit(Bitrate, "Tbps")
tbps = Tbps
TBPS = Tbps
Tibps = _Unit(Bitrate, "Tibps")
tibps = Tibps
TIBPS = Tibps

# units generated by:
# from toscaparser.elements.scalarunit import ScalarUnit_Size, ScalarUnit_Time, ScalarUnit_Frequency, ScalarUnit_Bitrate

# for scalar_cls in [ScalarUnit_Size, ScalarUnit_Time, ScalarUnit_Frequency, ScalarUnit_Bitrate]:
#     unit_class = scalar_cls.__name__[len("ScalarUnit_"):]
#     for name, scale in scalar_cls.SCALAR_UNIT_DICT.items():
#         print(f'{name} = _Unit({unit_class}, "{name}")')
#         if name != name.lower():
#             print(f"{name.lower()} = {name}")
#         if name != name.upper():
#             print(f"{name.upper()} = {name}")


def unit(unit: str) -> _Unit:
    obj = globals()[unit]
    assert isinstance(obj, _Unit)
    return obj


_unit = unit


def scalar(val) -> Optional[_Scalar]:
    val, unit = parse_scalar_unit(val)
    if val is not None and unit:
        return val * _unit(unit)
    return None


def scalar_value(val_, unit=None, round=None) -> Union[float, int, None]:
    val, valunit = cast(
        Tuple[Optional[Union[int, float]], Optional[str]], parse_scalar_unit(val_)
    )
    if val is None:
        return val
    if unit is None:
        newval = val
    else:
        if valunit:
            unit_val = _unit(valunit).value
            val = val * unit_val
        unit_val = _unit(str(unit)).value
        newval = val / unit_val
    if round == "ceil":
        return math.ceil(newval)
    elif round == "floor":
        return math.floor(newval)
    elif round is not None:  # assume a number
        return builtins.round(newval, round)
    else:
        if abs(newval) < 1:
            return newval
        return builtins.round(newval)
