import arc
import copy
from datetime import datetime
import elecsus
import importlib
import logging
import matplotlib.pyplot as plt
import numpy as np
import os
import pkgutil
import sys
import scipy as sp
from scipy import constants as c
from scipy.linalg import eig, eigh
from scipy.interpolate import RectBivariateSpline
import seaborn as sns
import sympy as sy
from sympy.physics.wigner import wigner_3j, wigner_6j
import symengine as se
import tqdm


def import_submodules(module):
    """Import all submodules of a module, recursively."""
    for loader, module_name, is_pkg in pkgutil.walk_packages(
            module.__path__, module.__name__ + '.'):
        importlib.import_module(module_name)
import_submodules(elecsus)


log = logging.getLogger('LME')
log.setLevel(logging.DEBUG)
logging.getLogger('matplotlib.font_manager').disabled = True


class state:
    def __init__(self, n, l, j, f=None):
        self.n = n
        self.l = l
        self.j = j
        self.f = f

    def __str__(self):
        symbols = ['S', 'P', 'D', 'F']
        return f'{self.n}{symbols[self.l]}{self.j}'

    def __call__(self, precision):
        if precision == 'nlj':
            return (self.n, self.l, self.j)
        elif precision == 'nljf':
            return self.n, self.l, self.j, self.f


class beam:
    def __init__(self, **kwargs):
        self.w = kwargs['w']
        if 'profile' in kwargs:
            self.profile = kwargs['profile']
        else:
            log.warning('Use Gaussian intensity profile')
            self.profile = 'gaussian'
        # beam diameter, either as diameter or area
        if 'D' in kwargs:
            self.D = kwargs['D']
            self.A = c.pi * kwargs['D']**2 / 4
        else:
            self.A = kwargs['A']
            self.D = 2 * np.sqrt(kwargs['A'] / c.pi)
        # beam power, either as power or electric field
        if 'E' in kwargs:
            self.setE(kwargs['E'])
        else:
            self.setP(kwargs['P'])

    def setP(self, P):
        self.P = P
        # I = 1/2 * c * epsilon_0 * E0**2
        log.info(f'Beam profile used: {self.profile}')
        if self.profile == 'flat':
            I = P / self.A
        elif self.profile == 'gaussian':
            I = 2 * P / self.A
        else:
            raise KeyError('no beam profile specified')
        self.E = np.sqrt(2 * I / c.c / c.epsilon_0)


    def setE(self, E):
        self.E = E
        self.P = self.A / 4 * c.c * c.epsilon_0 * E**2


    def __iter__(self):
        return iter((self.w, self.P, self.D))


p_dict_defaults = {	'lcell':75e-3,'Bfield':0., 'T':20.,
                    'GammaBuf':0., 'shift':0.,
                    # Polarisation of light
                    'theta0':0., 'Pol':50.,
                    # B-field angle w.r.t. light k-vector
                    'Btheta':0, 'Bphi':0,
                    'Constrain':True, 'DoppTemp':20.,
                    'rb85frac':72.17, 'K40frac':0.01, 'K41frac':6.73,
                    # Beyond weak fields
                    'laserPower': 1e-15, 'laserWaist': 5e-3,
                    'BoltzmannFactor':True}


def get_spectra(X, E_in, p_dict, outputs=None):
    '''
    This function acts as a wrapper, providing a similar interface as the function of ElecSus with the same name.
    Alternatively, direct interaction with the 'atomicSystem' class is possible;
    This allows to modify the assumed beam shape (Gaussian vs. flat)
    '''
    Elem = p_dict['Elem']
    # Insert all default values we did not explicitly specify
    p_dict = {**p_dict_defaults, **p_dict}
    if Elem=='Li':
        mass_numbers = [6, 7]
        fractions = [7.4, 92.6]
    if Elem=='Na':
        mass_numbers = [23]
        fractions = [100]
    if Elem=='K':
        mass_numbers = [39, 40, 41]
        fractions = [100 - p_dict['K40frac'] - p_dict['K41frac'], p_dict['K40frac'], p_dict['K41frac']]
    if Elem=='Rb':
        mass_numbers = [85, 87]
        fractions = [p_dict['rb85frac'], 100 - p_dict['rb85frac']]
    if Elem=='Cs':
        mass_numbers = [133]
        fractions = [100]
    atoms = []

    log.debug('Generate atoms...')
    for mass_number, fraction in zip(mass_numbers, fractions):
        if fraction > 0:
            atom = atomicSystem(f'{Elem}{mass_number}', E_in, p_dict)
            atom.abundance = fraction / 100
            atoms.append(atom)
    useDoppler = True if p_dict['DoppTemp'] > -273.14 else False
    beam_ge=beam(w=(X-p_dict['shift']), P=p_dict['laserPower'], D=p_dict['laserWaist'])
    transmissions = [atom.transmission([beam_ge], z=p_dict['lcell'], doppler=useDoppler) for atom in atoms]
    S0 = np.prod(np.array(transmissions), axis=0)
    return S0


class atomicSystem:
    def __init__(self, element, E_in, p_dict, states=None):
        self.element = element
        if element.lower() in ['li6', 'lithium6']:
            self.atom = arc.Lithium6()
        elif element.lower() in ['li7', 'lithium7']:
            self.atom = arc.Lithium7()
        elif element.lower() in ['na', 'na23', 'sodium', 'sodium23']:
            self.atom = arc.Sodium()
        elif element.lower() in ['k39', 'potassium39']:
            self.atom = arc.Potassium39()
            self.abundance = 1 - (p_dict['K40frac'] + p_dict['K41frac']) / 100
        elif element.lower() in ['k40', 'potassium40']:
            self.atom = arc.Potassium40()
            self.abundance = p_dict['K40frac'] / 100
        elif element.lower() in ['k41', 'potassium41']:
            self.atom = arc.Potassium41()
            self.abundance = p_dict['K41frac'] / 100
        elif element.lower() in ['rb85', 'rubidium85']:
            self.atom = arc.Rubidium85()
            self.abundance = p_dict['rb85frac'] / 100
            self.meltingPoint = self.atom.meltingPoint
        elif element.lower() in ['rb87', 'rubidium87']:
            self.atom = arc.Rubidium87()
            self.abundance = 1 - p_dict['rb85frac'] / 100
            self.meltingPoint = self.atom.meltingPoint
        elif element.lower() in ['cs', 'cs133', 'caesium', 'caesium133']:
            self.atom = arc.Caesium()
        else:
            raise ValueError

        if states is None:
            groundState = state(self.atom.groundStateN, 0, 1/2)
            if p_dict['Dline'] == 'D1':
                excitedState = state(self.atom.groundStateN, 1, 1/2)
            elif p_dict['Dline'] == 'D2':
                excitedState = state(self.atom.groundStateN, 1, 3/2)
            else:
                raise ValueError
            self.states = [groundState, excitedState]
        else:
            self.states = states
            # override Dline in dict, if explicit states have been defined
            log.warning('States have been explicitly given, override Dline parameter')
            if (states[1].j - states[1].j) < 0.9:
                p_dict['Dline'] = 'D1'
            elif (states[1].j - states[1].j) < 1.1:
                p_dict['Dline'] = 'D2'
            else:
                raise ValueError('Unsupported Dline')

        self.n_states = len(self.states)

        self.T = p_dict['T'] + 273.15
        if p_dict['Constrain'] == True:
            self.DoppT = self.T
        else:
            self.DoppT = p_dict['DoppTemp'] + 273.15
        self.beam_diameter = p_dict['laserWaist']
        self.E_in = E_in
        self.p_dict = p_dict

        log.debug('Init system properties')
        self.initSystemProperties()
        log.debug('Generate symbols')
        self.generateSymbols()
        log.debug('Genrate matrices')
        self.generateMatrices()
        # Add constrain that total population has to be 1
        self.system_matrix = self.master_equation.as_mutable()
        if 'symbolic_transit' in self.p_dict:
            log.warning('USING symbolic transit time')
            self.system_matrix = self.system_matrix.subs({'tau_t': self.transit_time})
        self.system_matrix[0] = -1 + self.r.trace()
        log.debug('Generate linear system')
        self.A, self.b = self.generate_linear_system()

    def update_transit(self, mean_speed):
        self.transit_time = self.getTransitTime(mean_speed) * 1e6
        log.info(f'Updated transit time: {self.transit_time}')
        # self.generateMatrices()
        self.system_matrix = self.master_equation.as_mutable()
        if 'symbolic_transit' in self.p_dict:
            self.system_matrix = self.system_matrix.subs({'tau_t': self.transit_time})
        self.system_matrix[0] = -1 + self.r.trace()
        self.A, self.b = self.generate_linear_system()

    def update(self, p_dict):
        self.p_dict = p_dict
        self.T = p_dict['T'] + 273.15
        if p_dict['Constrain'] == True:
            self.DoppT = self.T
        else:
            self.DoppT = p_dict['DoppTemp'] + 273.15
        self.beam_diameter = p_dict['laserWaist']
        self.transit_time = self.getTransitTime()

    def getTransitTime(self, mean_speed_2d=None):
        # Refs: ARC-Alkali-Rydberg-Calculator Web interface (click 'View code')
        # but here we use the definitions from Sagle 1996
        if mean_speed_2d is None:
            mean_speed_2d = np.sqrt(np.pi * c.k * self.DoppT / 2 / self.atom.mass)
        mean_path = np.pi / 4 * self.beam_diameter
        tau = mean_path / np.abs(mean_speed_2d)
        return tau

    def Hamiltonian(self, n, l, j, B):
        if B == 0.0:
            B += 1e-9 # avoid degeneracy problem..?
        # self.atom.UsedModulesARC.hyperfine = True
        Ahfs, Bhfs = self.atom.getHFSCoefficients(n, l, j)

        # Bohr Magneton
        uB = c.physical_constants["Bohr magneton in Hz/T"][0]

        # Define Spin Matrices
        N = round((2 * j + 1) * (2 * self.atom.I + 1))
        [jx, jy, jz] = self.atom._spinMatrices(j)
        ji = np.eye(round(2.0 * j + 1.0))
        [ix, iy, iz] = self.atom._spinMatrices(self.atom.I)
        ii = np.eye(round(2.0 * self.atom.I + 1.0))

        # Calculate Tensor Products
        Jx = np.kron(jx, ii)
        Jy = np.kron(jy, ii)
        Jz = np.kron(jz, ii)
        Ix = np.kron(ji, ix)
        Iy = np.kron(ji, iy)
        Iz = np.kron(ji, iz)
        J2 = Jx**2 + Jy**2 + Jz**2
        I2 = Ix**2 + Iy**2 + Iz**2
        IJ = Ix * Jx + Iy * Jy + Iz * Jz
        # F Basis
        Fx = Jx + Ix
        Fy = Jy + Iy
        Fz = Jz + Iz
        F2 = Fx**2 + Fy**2 + Fz**2

        # Hyperfine Interaction
        Hhfs = Ahfs * IJ
        if Bhfs != 0:
            Hhfs += (
                Bhfs
                * (3 * IJ * IJ + 3 / 2 * IJ - I2 * J2)
                / (2 * self.atom.I * (2 * self.atom.I - 1) * 2 * j * (2 * j - 1))
            )

        # Zeeman Interaction
        Hz = uB * (self.atom.getLandegjExact(l, j) * Jz + self.atom.gI * Iz)

        # ctr = -1
        # for b in B:
            # ctr = ctr + 1
        eVal, eVec = eigh(Hhfs + B * Hz)
        eVal *= 1e-6
            # en[ctr, :] = eVal
        stateManifold = np.append([eVal.real], eVec, axis=0)
        sortedManifold = sorted(np.transpose(stateManifold),key=(lambda i:i[0]))
        return sortedManifold, eVal.real

    def initSystemProperties(self):
        self.f_resonance = self.atom.getTransitionFrequency(
            *self.states[0]('nlj'), *self.states[1]('nlj'))

        self.f = [self.atom.breitRabi(*state('nlj'), np.array([self.p_dict['Bfield']]))[1] for state in self.states]
        self.mf = [self.atom.breitRabi(*state('nlj'), np.array([self.p_dict['Bfield']]))[2] for state in self.states]

        self.n = np.array([len(mf) for mf in self.mf])
        self.total_levels = sum([len(mf) for mf in self.mf])

        self.transit_time = self.getTransitTime() * 1e6
        if 'Gammat' in self.p_dict:
            self.transit_time = self.p_dict['Gammat'] * 1e6

        self.slices = [slice(self.n[0:i].sum(), self.n[0:i+1].sum()) for i in range(self.n_states)]
        # DME = np.sqrt((2*self.states[0].j+1) / (2*self.states[1].j+1)) \
        #     * self.atom.getReducedMatrixElementJ_asymmetric(*self.states[0]('nlj'), *self.states[1]('nlj')) \
        #     * c.e * c.physical_constants['Bohr radius'][0]
        # g = c.e * c.physical_constants['Bohr radius'][0]

        # See PhD thesis of Zentile, wigner 3-j symbol is 1/sqrt(3) for all combinations of q, ml,ml'
        DME = self.atom.getRadialMatrixElement(*self.states[0]('nlj'), *self.states[1]('nlj')) \
            * c.e * c.physical_constants['Bohr radius'][0] * np.sqrt(1/3)

        self.naturalLineWidth = [self.atom.getTransitionRate(
            *self.states[i+1]('nlj'), *self.states[i]('nlj')) / 2 / c.pi * 1e-6
            for i in range(self.n_states-1)]
        # SFF = [self.getSFF(self.states[i].F(self.sublevels[i]),
        #                    self.states[i+1].F(self.sublevels[i+1]))
        #        for i in range(self.n_states-1)]
        # self.dipole_moments = [np.sqrt(SFF[i]/3) * DME[i]
        #                        for i in range(self.n_states-1)]
        H = elecsus.libs.EigenSystem.Hamiltonian(self.element, self.p_dict['Dline'], self.atom.gL, self.p_dict['Bfield'])


        # elif (Trans=='D2') and (Isotope=='K41'):
        #     transition = KD2Transition
        #     atom_transition = K41_D2

        # self.groundManifold, self.groundEnergies = self.groundStateManifold(atom.gI,atom.I,atom.As,
        #                         atom_transition.IsotopeShift,Bfield)
        # self.excitedManifold, self.excitedEnergies = self.excitedStateManifold(gL,atom.gI,atom.I,
        #                         atom_transition.Ap,atom_transition.Bp,Bfield)

        # arcM, arcV = self.Hamiltonian(5, 0, 1/2, self.p_dict['Bfield']/10000)
        # eleM = np.array(H.groundManifold)
        # eleV = H.groundEnergies
        # arcM = np.array(arcM)
        # print(eleM.real.round(3))
        # print(arcM.real.round(3))
        # print(eleV)
        # print(arcV)
        # print(np.allclose(np.abs(eleM), np.abs(arcM)))
        # print(np.allclose(eleV, arcV))
        # print(eleM)
        # print(arcM)
        # sys.exit()



        Mg = np.array(H.groundManifold)[:,1:]  # Cut off energy eigenvalues
        Me = np.array(H.excitedManifold)[:,1:]

        self.energySeparation = [None] * self.n_states
        self.energySeparation[0] = H.groundEnergies
        if self.p_dict['Dline'] == 'D1':
            DlineIndexOffset = 0
            self.energySeparation[1] = H.excitedEnergies[0:self.n[0]]
        elif self.p_dict['Dline'] == 'D2':
            DlineIndexOffset = self.n[0]
            self.energySeparation[1] = H.excitedEnergies[self.n[0]:]

        # print(np.array(H.groundManifold).real)
        # a = np.array(H.excitedManifold)[DlineIndexOffset:DlineIndexOffset+self.n[1], 0].real
        # b = np.array(H.excitedManifold)[DlineIndexOffset:DlineIndexOffset+self.n[1], 0*self.n[0]+1:1*self.n[0]+1].real
        # a = a[:,None]
        # print(np.concatenate([a, b], axis=1))
        # print(self.p_dict['Pol'])
        # sys.exit()

        self.eigv = np.diag(np.ones(self.total_levels))

        dme_r = np.matmul(Mg, Me[DlineIndexOffset:DlineIndexOffset+self.n[1], 0*self.n[0]:1*self.n[0]].T).real
        dme_z = np.matmul(Mg, Me[DlineIndexOffset:DlineIndexOffset+self.n[1], 1*self.n[0]:2*self.n[0]].T).real
        print(dme_z)
        dme_l = np.matmul(Mg, Me[DlineIndexOffset:DlineIndexOffset+self.n[1], 2*self.n[0]:3*self.n[0]].T).real
        dme_squared = np.power(dme_r, 2) + np.power(dme_z, 2) + np.power(dme_l, 2)
        self.Gammas = dme_squared * self.naturalLineWidth[0]

        # E_in_lrz = elecsus.libs.BasisChanger.xyz_to_lrz(self.E_in)

        # E_L = elecsus.libs.BasisChanger.lrz_to_xyz([1,0,0])
        # E_R = elecsus.libs.BasisChanger.lrz_to_xyz([0,1,0])
        # E_0 = elecsus.libs.BasisChanger.lrz_to_xyz([0,0,1])
        # print(np.abs(np.dot(self.E_in, E_L.conj()))**2)

        # Calculate the projection of E_in onto the lrz basis
        E_in_lrz = elecsus.libs.BasisChanger.xyz_to_lrz(self.E_in)
        polarization_contributions = np.abs(E_in_lrz@np.identity(3))**2

        self.dme = np.sqrt(polarization_contributions[0]) * dme_l \
                 + np.sqrt(polarization_contributions[1]) * dme_r \
                 + np.sqrt(polarization_contributions[2]) * dme_z
        self.dme *= DME

    def generateSymbols(self):
        #######################################################################
        # Generate symbols and variables
        #######################################################################
        self.wL = sy.symbols(f'w_01')
        self.Efield = sy.symbols(f'Efield_01')
        self.r_individual = sy.symbols(
            f'\\rho_{{(0:{self.total_levels})/(0:{self.total_levels})}}')
        self.r = sy.Matrix(self.total_levels,
                           self.total_levels, self.r_individual)
        if 'symbolic_transit' in self.p_dict:
            self.tau_t = sy.symbols('tau_t')

    def generateMatrices(self):
        #######################################################################
        # Generate matrices
        #######################################################################
        self.H_rabi = sy.zeros(self.total_levels, self.total_levels)
        self.H_rabi[self.slices[0], self.slices[1]] = 0.5e-6 / c.h * self.dme * self.Efield
        self.H_rabi = self.H_rabi + self.H_rabi.transpose()

        detunings = np.concatenate([-self.energySeparation[0], self.wL - self.energySeparation[1]])
        self.H_energylevels = sy.diag(*detunings)
        self.H = self.H_rabi + self.H_energylevels

        # Make Lindblad
        def Lindblad_decay(rates):
            L = sy.zeros(self.total_levels, self.total_levels)
            for i in range(self.total_levels):
                for j in range(self.total_levels):
                    c = sy.Matrix(np.outer(self.eigv[i], self.eigv[j]).T)
                    # L += rates[i,j] * (c@self.r@c.T - 0.5 * (c.T@c@self.r + self.r@c.T@c))
                    # L += (rates[j, i] * self.r[j, j] - rates[i, j] * self.r[i, i]) * sy.Matrix(np.outer(self.eigv[i], self.eigv[i]))
                    L[i,i] += (rates[j, i] * self.r[j, j] - rates[i, j] * self.r[i, i])
                    if (i != j):
                        for k in range(self.total_levels):
                            # L -= 0.5 * (rates[i, k] + rates[j, k]) * self.r[i, j] * sy.Matrix(np.outer(self.eigv[i], self.eigv[j]))
                            L[i,j] -= 0.5 * (rates[i, k] + rates[j, k]) * self.r[i, j]
            return L

        def Lindblad_dephase(rates):
            L = sy.zeros(self.total_levels, self.total_levels)
            for i in range(self.total_levels):
                for j in range(self.total_levels):
                    if (i != j):
                        L[i, j] -= 0.5 * rates[i, j] * self.r[i, j]
            return L

        # defining the correct Lindblad operators for transit-time and collisional broadening
        g_dec = np.zeros((self.total_levels, self.total_levels))
        g_col = np.zeros((self.total_levels, self.total_levels))
        g_transit = np.zeros((self.total_levels, self.total_levels))
        # Putting the decay in this part of the block matrix, we get decay.
        # If we would put it into the other one, we would get spontaneous excitation
        g_dec[self.slices[1], self.slices[0]] = self.Gammas.T

        # Additional division by two, so that all coherence terms in the Lindblad have correctly 0.5 * Gamma
        g_transit[self.slices[0], self.slices[0]] = 1 / self.transit_time / self.n[0] / 2
        # "Decay" of excited states to ground states due to transit time
        g_transit[self.slices[1], self.slices[0]] = 1 / self.transit_time / self.n[0] / 2
        if 'symbolic_transit' in self.p_dict:
            g_transit = sy.zeros(self.total_levels, self.total_levels)
            g_transit[self.slices[0], self.slices[0]] = 1 / self.tau_t / self.n[0] / 2 * sy.ones(self.n[0], self.n[0])
            # "Decay" of excited states to ground states due to transit time
            g_transit[self.slices[1], self.slices[0]] = 1 / self.tau_t / self.n[0] / 2 * sy.ones(self.n[1], self.n[0])

            # g_tmp = sy.zeros(self.total_levels, self.total_levels)
            # g_tmp[self.slices[0], self.slices[0]] = self.tau_t * sy.ones(self.n[0], self.n[0]) / self.total_levels
            # g_tmp[self.slices[1], self.slices[0]] = self.tau_t * sy.ones(self.n[1], self.n[0]) / self.total_levels
            # with open("L_transit_tmp.txt", "w") as outf:
            #     L_tmp = Lindblad_decay(g_tmp)
            #     outf.write(latex(L_tmp))
            # sys.exit()

        if 'collisions' not in self.p_dict:
            log.warning('Implicitly assume decaying collisions')
            self.p_dict['collisions'] = 'decay'
        if self.p_dict['collisions'] == 'decay':
            log.info('decaying collisions!')
            # g_col[self.slices[0], self.slices[0]] = self.p_dict['GammaBuf'] / self.n[0]
            g_col[self.slices[1], self.slices[0]] = self.p_dict['GammaBuf'] / self.n[0]
            L_dec = Lindblad_decay(g_dec + g_transit + g_col)
            self.master_equation = -sy.I * (self.H*self.r - self.r*self.H) - L_dec
        elif self.p_dict['collisions'] == 'dephase':
            log.info('dephasing collisions!')
            # g_col[self.slices[0], self.slices[0]] = self.p_dict['GammaBuf']
            g_col[self.slices[1], self.slices[0]] = self.p_dict['GammaBuf']
            g_col[self.slices[0], self.slices[1]] = self.p_dict['GammaBuf']
            L_dec = Lindblad_decay(g_dec + g_transit)
            L_deph = Lindblad_dephase(g_col)
            self.master_equation = -sy.I * (self.H*self.r - self.r*self.H) - L_dec - L_deph

    def generate_linear_system(self):
        self.r_list = self.matrix2list(self.r)
        test = np.array(self.r_list)
        # Create list of off-diagonal elements relevant for i->j transition
        self.transition_list = []
        for i in range(self.n_states-1):
            mask = np.full((self.total_levels, self.total_levels), False)
            mask[self.slices[i], self.slices[i+1]] = True
            self.transition_list.append(self.matrix2list(mask))
        self.transition_list2 = []
        for i in range(self.n_states-1):
            mask = np.full((self.total_levels, self.total_levels), False)
            mask[self.slices[i+1], self.slices[i]] = True
            self.transition_list2.append(self.matrix2list(mask))

        # m = np.array(self.transition_list2).squeeze()
        # print(test[m])
        # print(m.shape)
        # sys.exit()
        A, b = sy.linear_eq_to_matrix(self.system_matrix, self.r_list)
        # A = A.simplify(rational=None)
        A = se.Matrix(A)
        A = se.Lambdify([self.wL, self.Efield], A, real=False, cse=True)
        # b is always just an vector with zeros and the first entry being one
        b = np.zeros((self.total_levels**2, 1))
        b[0] = 1
        return A, b

    def v_dist(self, v):
        return np.sqrt(self.atom.mass / (2 * c.pi * c.k * self.DoppT)) \
            * np.exp(-self.atom.mass * v**2 / (2 * c.k * self.DoppT))

    def rayleigh(self, v):
        # https://en.wikipedia.org/wiki/Rayleigh_distribution
        return 2 * c.pi * self.atom.mass / (2 * c.pi * c.k * self.DoppT) \
        * np.exp(-self.atom.mass * v**2 / (2 * c.k * self.DoppT)) * v

    def cdf(self, v):
        o = np.sqrt(c.k * self.DoppT / self.atom.mass) * np.sqrt(2)
        return 0.5 * (1 + sp.special.erf(v/o))

    def cdfinv(self, p):
        o = np.sqrt(c.k * self.DoppT / self.atom.mass) * np.sqrt(2)
        return o * sp.special.erfinv(2 * p - 1)

    def matrix2list(self, mat):
        # Generate list containing all entries of density matrix
        # First diagonal entries: r[0,0], r[1,1], ...
        # Then upper half: r[0,1], r[0,2], ...
        # Then lower half: r[1,0], r[2,0], ...
        l1 = []
        l2 = []
        for i in range(mat.shape[0]):
            for j in range(i+1, mat.shape[1]):
                l1.append(mat[i, j])
                l2.append(mat[j, i])
        return list(mat.diagonal()) + l1 + l2

    def solve(self, beams):
        #######################################################################
        # Calculate Rabi Frequencies
        #######################################################################
        f_list, _, _ = zip(*beams)
        E_list = [np.atleast_1d(beam.E) for beam in beams]
        w_ge = np.atleast_1d(f_list[0])
        wavenumber_ge = self.f_resonance / c.c

        #######################################################################
        # Solve linear system
        #######################################################################
        log.debug('Solve linear system')
        res = np.array([[np.linalg.solve(self.A(w, E), self.b) for E in E_list[0]] for w in w_ge])

        #######################################################################
        # Extract relevant information
        #######################################################################
        # Move density matrix dimension to the front
        res = np.moveaxis(res.squeeze(), -1, 0)
        # If we only want to calculate a single value, res[list] * k
        # would multiply wrong dimensions, e.g. (8,) * (8,1) -> (8,8)
        # So we add a dimension so that (8,1) * (8,1) -> (8,1)
        if res.ndim == 1:
            res = np.expand_dims(res, axis=1)

        k_alt = [np.divide.outer(self.dme.ravel(), E_list[i]) / c.epsilon_0 for i in range(self.n_states-1)]
        # - Return sum of excited states. Indexing order is given by order
        #   of arguments of 'sy.linear_eq_to_matrix' above
        # - Population of excited states is given by diagonal entries
        # - Complex-valued susceptibility is given by off-axis entries
        #   (only one side, they are complex conjugated anyway)
        state_population = np.array([np.sum(res[self.slices[i]], axis=0).real for i in range(self.n_states)])
        chi = np.array([self.abundance * 2 * np.sum(res[self.transition_list[i]] * k_alt[i], axis=0)
            for i in range(self.n_states-1)])
        # chi = np.array([self.abundance * (
        #     np.sum(res[self.transition_list[i]] * k_alt[i], axis=0)
        #     -np.sum(res[self.transition_list2[i]] * k_alt[i], axis=0))
        #     for i in range(self.n_states-1)])
        return state_population[1].squeeze(), chi[0].squeeze()

    def solve_w_doppler(self, beams):
        log.debug('enter __solve_w_doppler__')
        beam_ge = beams[0]
        # chi_dopp(∆) = \int p(v,T)chi(∆-kv)dv = (p*chi)(∆)
        # k = 1 / lam2bda = w/c
        w_ge, P_ge, D_ge = beam_ge
        w_ge = np.atleast_1d(w_ge)
        P_ge = np.atleast_1d(P_ge)
        k_ge = self.f_resonance / c.c / 1e6

        exci_state = np.ones((len(w_ge), len(P_ge)), dtype='float64')
        chi = np.zeros_like(exci_state, dtype='complex128')

        resolution = 2  # MHz
        v = np.arange(w_ge.min()/k_ge - 1000, w_ge.max()/k_ge + 1000, resolution)
        v_distribution = self.v_dist(np.subtract.outer(w_ge / k_ge, v))
        for i, P in enumerate(P_ge):
                # Use symmetry of convolution to calculate population_number
                # once and instead calculate Maxwell Boltzmann distribution
                # more often (which is computationally cheaper)
                E, C = self.solve([beam(w=k_ge * v, P=P, D=D_ge, profile=beam_ge.profile)])
                exci_state[:, i] = np.sum(v_distribution * E, axis=1) * resolution
                chi[:, i] = np.sum(v_distribution * C, axis=1) * resolution
        return exci_state.squeeze(), chi.squeeze()

    def transmission(self, beams, z=50e-3, doppler=True, transit_type='single'):
        log.debug('__enter transmission__')
        alpha = self.optical_depth(beams, doppler, transit_type=transit_type)
        return np.exp(alpha * z)

    def absorption(self, beams, z=50e-3, doppler=True, transit_type='single'):
        return 1 - self.transmission(beams, z, doppler, transit_type=transit_type)

    def optical_depth(self, beams, doppler=True, transit_type='single'):
        log.debug('__enter optical_depth__')
        n = self.atom.getNumberDensity(self.T)

        if (transit_type == 'integral') and ('symbolic_transit' not in self.p_dict):
            log.warning('Integrating without symbolic transit time not supported.')
            log.warning('Change your p_dict to include "symbolic_transit: True"')
            log.warning('Falling back to average operation...')
            transit_type = 'single'

        if transit_type == 'single':
            if doppler:
                _, chi = self.solve_w_doppler(beams)
            else:
                _, chi = self.solve(beams)
        elif transit_type == 'integral':
            v = np.linspace(0, 900, 25)
            dv = v[1] - v[0]
            # chi = np.zeros((beams[0].w.size, v.size), dtype=np.complex128)
            chi = 0
            for i, vi in enumerate(v):
                self.update_transit(vi)
                if doppler:
                    _, tmp_chi = self.solve_w_doppler(beams)
                else:
                    _, tmp_chi = self.solve(beams)
                chi += tmp_chi * self.rayleigh(vi) * dv

        n_imag = np.sqrt(1.0 + chi * n).imag
        return 4 * c.pi * self.f_resonance / c.c * n_imag

    def propagated_transmission(self, beams, z=50e-3, doppler=True, steps=50, transit_type='single'):
        # This function so far only calculates the transmission values for the peak absorption of the selected detuning range
        w, P0, D, _ = beams[0]
        dz = z / steps
        P = np.zeros((steps+1, len(w)))
        T = np.ones((steps+1, len(w)))
        P[0] = P0

        for i in range(1, steps+1):
            T[i] = self.transmission([beam(w=w, P=P[i-1].min(), D=D, profile=beams[0].profile)], z=dz, doppler=doppler, transit_type=transit_type)
            P[i] = T[i] * P[i-1]
        T = np.product(T, axis=0)
        return T
        # detunings, powers, beam_diameter, chi = self.LUT()
        # chi_real = RectBivariateSpline(detunings, powers, chi.real, kx=1, ky=1)
        # chi_imag = RectBivariateSpline(detunings, powers, chi.imag, kx=1, ky=1)

        # P0 = P * (beam_diameter / D)#**2
        # n = self.atom.getNumberDensity(self.T)
        # k = self.f_resonance / c.c
        # resolution = 2  # MHz
        # v = np.arange(w.min()/1e6 - 1000, w.max()/1e6 + 1000, resolution)
        # v_distribution = self.v_dist(np.subtract.outer(w / k, v))
        # # Initialize some variables
        # P = np.zeros((steps+1, len(beams[0].w)))
        # T = np.ones((steps+1, len(beams[0].w)))
        # P[0] = P0
        # abs_pref = dz * 4 * c.pi * self.f_resonance / c.c

        # for i in range(1, steps+1):
        #     # RectBivariateSpline wants strictly increasing values for x and y.
        #     # The detuning fulfills this naturally, but power not.
        #     # So we sort it and later "unsort" it again
        #     if doppler:
        #         sequence = np.argsort(P[i-1])
        #         chi_t = chi_real(k*v, P[i-1][sequence], grid=True) + 1j * chi_imag(k*v, P[i-1][sequence], grid=True)
        #         chi = np.sum(v_distribution * chi_t.T[sequence.argsort()], axis=1) * resolution
        #     else:
        #         chi = chi_real(w, P[i-1], grid=False) + 1j * chi_imag(w, P[i-1], grid=False)
        #     T[i] = np.exp(abs_pref * np.sqrt(1. + chi * n).imag)
        #     P[i] = T[i] * P[i-1]
        # T = np.product(T, axis=0)
        # return T


if __name__ == '__main__':
    sns.set_palette(sns.color_palette('tab20'))
    p_dict = {
        'Elem':'Rb','Dline':'D2', 'lcell':2e-3, 'T': 20.,
	   'Bfield': 100, 'rb85frac': 0, 'Constrain': False, 'DoppTemp': -273.1499,
       'laserPower': 1e-15, 'laserWaist': 2e-3}
    # groundState = state(5, 0, 1/2)
    # excitedState = state(5, 1, 3/2)
    # rb85 = atomicSystem('Rb87', p_dict)
    x = np.linspace(3800, 4600, 500)
    # od = rb85.transmission([beam(w=x, P=1e-15, D=5e-3)], z=2e-3, doppler=False)
    E_LCP = elecsus.libs.BasisChanger.lrz_to_xyz([1,0,0])
    E_RCP = elecsus.libs.BasisChanger.lrz_to_xyz([0,1,0])
    E_LP = np.array([1,0,0])

    y_ele1 = elecsus.elecsus_methods.calculate(x, E_in=E_LCP, p_dict=p_dict, outputs=['S0'])[0]
    y_bwf1 = get_spectra(x, E_in=E_LCP, p_dict=p_dict)
    y_ele2 = elecsus.elecsus_methods.calculate(x, E_in=E_RCP, p_dict=p_dict, outputs=['S0'])[0]
    y_bwf2 = get_spectra(x, E_in=E_RCP, p_dict=p_dict)
    y_ele3 = elecsus.elecsus_methods.calculate(x, E_in=E_LP, p_dict=p_dict, outputs=['S0'])[0]
    y_bwf3 = get_spectra(x, E_in=E_LP, p_dict=p_dict)

    plt.figure()
    plt.plot(x, y_ele1)
    plt.plot(x, y_bwf1, '--')
    plt.plot(x, y_ele2)
    plt.plot(x, y_bwf2, '--')
    plt.plot(x, y_ele3)
    plt.plot(x, y_bwf3, '--')
    plt.show()
