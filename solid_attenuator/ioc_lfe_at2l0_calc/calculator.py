"""
Independent filter calculation code - no IOC logic should be mixed in here.

References
----------
B.L. Henke, E.M. Gullikson, and J.C. Davis,
X-ray interactions: photoabsorption, scattering, transmission, and reflection
at E=50-30000 eV, Z=1-92,
Atomic Data and Nuclear Data Tables 54 no.2, 181-342 (July 1993).

B.D. Cullity, Elements of X-Ray Diffraction (Second Edition), 11-20, (1978).
"""

import copy
import enum
import functools
import itertools
import pathlib
import typing
from typing import Tuple

import numpy as np
import periodictable
import scipy.constants
from scipy.interpolate import interp1d

CXRO_PATH = pathlib.Path(__file__).parent / 'CXRO'

# TODO: NOTE: these are for overriding data that comes from periodictable.
filter_data = {
    'C': {
        'density': 3.51E6,       # grams/m^3 (**diamond**)
    },
}


class ConfigMode(enum.Enum):
    Floor = enum.auto()
    Ceiling = enum.auto()


@functools.lru_cache(maxsize=32, typed=False)
def in_out_combinations(num_blades: int):
    """
    All possible in/out state configurations of ``N`` attenuator blades.

    Returns
    -------
    np.ndarray
        Of size 2 ** num_blades, with all possible combinations of inserted (1)
        and removed/stuck (nan).
    """
    return np.asarray(list(itertools.product([np.nan, 1], repeat=num_blades)))


class Config:
    def __init__(self, all_transmissions, filter_states, transmission):
        self.all_transmissions = copy.copy(all_transmissions)
        self.filter_states = copy.copy(filter_states)
        self.transmission = copy.copy(transmission)

    def __repr__(self):
        return (
            f'<Config {self.filter_states} transmission={self.transmission}>'
        )

    def __str__(self):
        """Format and print this configuration."""
        width = 80
        return '\n'.join((
            "-" * width,
            f"Calculated transmission value: {self.transmission}",
            "-" * width,
            str(self.filter_states),
            "=" * width,
        ))


def find_configs(
        all_transmissions: typing.List[float],
        t_des: float,
        t_base: float = 1.0,
        ) -> typing.List[Config]:
    """
    Find the optimal configurations for attaining desired transmission
    ``t_des`` at the current photon energy.

    Returns configurations which yield closest highest and lowest
    transmissions and their filter configurations.

    Parameters
    ----------
    all_transmissions : list of (float or nan)
        Basis vector of all filter transmission values.
        Note: Stuck filters should have transmission of `NaN`.

    t_des : float
        Desired transmission value.
    """

    config_table = in_out_combinations(len(all_transmissions))

    # Table of transmissions for all configurations is obtained by multiplying
    # basis by configurations in/out state matrix.
    t_table = np.nanprod(all_transmissions * config_table,
                         axis=1)

    # Create a table of configurations and their associated beam transmission
    # values, sorted by transmission value.
    configs = np.asarray([t_table, np.arange(len(config_table))])

    # Sort based on transmission value, retaining index order:
    sort_indices = configs[0, :].argsort()
    t_config_table = configs.T[sort_indices]

    # Find the index of the filter configuration which minimizes the
    # differences between the desired and closest achievable transmissions.
    idx_closest = np.argmin(np.abs(t_config_table[:, 0] - t_des))

    def get_config_and_transmission(idx: int) -> Tuple[np.ndarray, float]:
        conf = config_table[int(t_config_table[idx, 1])]
        transmission = np.nanprod(all_transmissions * conf)
        return conf, transmission

    # Obtain the optimal filter configuration and its transmission.
    closest, t_closest = get_config_and_transmission(idx_closest)

    # Determine the optimal configurations for "best highest" and "best lowest"
    # achievable transmissions.
    if t_closest == t_des:
        # The optimal configuration achieves the desired transmission exactly.
        idx_low = idx_closest
        idx_high = idx_closest
    elif t_closest < t_des:
        idx_low = idx_closest
        idx_high = min((idx_closest + 1, len(t_config_table) - 1))
    elif t_closest > t_des:
        idx_low = max((idx_closest - 1, 0))
        idx_high = idx_closest

    config_low, t_best_low = get_config_and_transmission(idx_low)
    config_high, t_best_high = get_config_and_transmission(idx_high)

    return [
        Config(all_transmissions=list(all_transmissions),
               filter_states=np.nan_to_num(config_low).astype(np.int),
               transmission=t_best_low),
        Config(all_transmissions=list(all_transmissions),
               filter_states=np.nan_to_num(config_high).astype(np.int),
               transmission=t_best_high)
    ]


def get_best_config(all_transmissions: typing.List[float],
                    t_des: float,
                    *,
                    mode: ConfigMode,
                    ) -> Config:
    """
    Return the optimal floor (lower than desired transmission) or ceiling
    (higher than desired transmission) configuration based on the current mode
    setting.
    """

    if isinstance(mode, str):
        mode = ConfigMode[mode]

    floor_config, ceil_config = find_configs(
        all_transmissions=all_transmissions, t_des=t_des)
    return floor_config if mode == ConfigMode.Floor else ceil_config


def find_closest_energy(photon_energy: float,
                        table: np.ndarray) -> typing.Tuple[float, int]:
    """
    Find the closest tabulated photon energy in the given table.

    Parameters
    ----------
    photon_energy : float
        The photon energy to find. [eV]

    table : np.ndarray
        The absorption table.

    Returns
    -------
    closest_energy : float
        The closest energy. [eV]

    closest_index : int
        The array index of the closest energy.
    """
    min_energy = table[0, 0]
    max_energy = table[-1, 0]
    energy_increment = (max_energy - min_energy) / table.shape[0]
    closest_idx = int(np.rint((photon_energy - min_energy) / energy_increment))
    if closest_idx < 0:
        closest_idx = 0
    if closest_idx >= table.shape[0]:
        closest_idx = -1  # Use greatest tabulated value.

    closest_eV = table[closest_idx, 0]
    return closest_eV, closest_idx


@functools.lru_cache()
def nff_to_npy(element):
    """
    Opens the .nff file containing scattering factors / energies for
    an atomic element and writes the data to a numpy array.

    Parameters
    ----------
    element : str
       Formula of the element to open e.g. "Si", "si", "C", "Au"
    """
    element = element.lower()
    return np.loadtxt(CXRO_PATH / f'{element}.nff', skiprows=1)


def _ev_linear(ev_low, ev_high, res=10, dec=2):
    """
    Return a linear range of photon energies.

    Parameters
    ----------
    ev_low : float
       Lower bound of photon energy range. [eV]

    ev_high : float
       Upper bound of photon energy range. [eV]

    res : float
       Magnitude of resolution.  Default of 10 yields 0.1 eV resolution.

    dec : int
       Decimal places.
    """
    num = int(ev_high - ev_low) * res + 1
    return np.around(np.linspace(ev_low, ev_high, num), dec)


def _fill_data_linear(element, ev_low, ev_high, res=10):
    """
    Interpolates data to add more samples.

    Parameters
    ----------
    element : str
       Formula of the element to open e.g. "Si", "si", "C", "Au"

    ev_low : float
       Lower bound of photon energy range. [eV]

    ev_high : float
       Upper bound of photon energy range. [eV]

    res : float
       Magnitude of resolution.  Default of 10 yields 0.1 eV resolution.
    """
    raw_data = nff_to_npy(element)
    new_range = _ev_linear(ev_low, ev_high, res=10)
    return interp1d(raw_data[:, 0], raw_data[:, 2])(new_range)


def get_absorption_table(formula: str,
                         ev_low: float = 10.,
                         ev_high: float = 30000., *,
                         atomic_weight: float = None,
                         density: float = None) -> np.ndarray:
    """
    Data table for photoabsorption calculations.

    Parameters
    ----------
    formula : str
       Formula of the element, e.g. "Si", "si", "C", "Au"

    ev_low : float
       Lower bound of photon energy range. [eV]

    ev_high : float
       Upper bound of photon energy range. [eV]

    atomic_weight : float, optional
        Atomic weight of ``formula``. [g]
        Required if information unavailable or incorrect in ``periodictable``
        dependency.

    density : float, optional
        Density of ``formula``. [g/m^3]
        Required if information unavailable or incorrect in ``periodictable``
        dependency.
    """
    if atomic_weight is None:
        try:
            atomic_weight = filter_data[formula]['atomic_weight']
        except KeyError:
            atomic_weight = periodictable.formula(formula).mass

    if density is None:
        try:
            density = filter_data[formula]['density']
        except KeyError:
            density = periodictable.formula(formula).density * 1e6
            # units: g/cm^3 -> m^3

    fs = _fill_data_linear(formula, ev_low, ev_high)
    table = np.zeros([fs.shape[0], 3])
    eV_space = _ev_linear(ev_low, ev_high)

    NA = scipy.constants.Avogadro
    c = scipy.constants.speed_of_light
    h, *_ = scipy.constants.physical_constants['Planck constant in eV/Hz']
    r0, *_ = scipy.constants.physical_constants['classical electron radius']

    table[:, 0] = eV_space[:]
    table[:, 1] = fs  # scattering factor f_2
    table[:, 2] = ((2 * r0 * h * c * fs/eV_space) * density *
                   (NA / atomic_weight))  # absorption constant \mu
    return table


def get_transmission(photon_energy: float,
                     table: np.ndarray,
                     thickness: float,
                     ) -> float:
    """
    Get transmission at the given energy with a filter.

    The filter is specified by the supplied absorption table and thickness,
    in units of meters.

    Parameters
    ----------
    photon_energy : float
        The photon energy to find. [eV]

    table : np.ndarray
        The absorption table.

    thickness : float
        Thickness of the filter. [m]

    Returns
    -------
    float
        Normalized transmission value.
    """
    _, idx = find_closest_energy(photon_energy, table)
    return np.exp(-table[idx, 2] * thickness)
