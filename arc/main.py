#!/usr/bin/env python
# encoding: utf-8

import logging
import sys
import os
import time
import re

from rmgpy.species import Species
from rmgpy.reaction import Reaction

from arc.settings import arc_path, default_levels_of_theory, check_status_command, servers
from arc.scheduler import Scheduler
from arc.exceptions import InputError
from arc.species import ARCSpecies
from arc.processor import Processor
from arc.job.ssh import SSH_Client

##################################################################


class ARC(object):
    """
    Main ARC object.
    The software is currently configured to run on a local computer, sending jobs / commands to one or more servers.

    The attributes are:

    ====================== ========== =========================================================================
    Attribute              Type       Description
    ====================== ========== =========================================================================
    `project`              ``str``    The project's name. Used for naming the working directory.
    'rmg_species_list'     ''list''   A list RMG Species objects. Species must have a non-empty label attribute
                                        and are assumed to be stab;e wells (not TSs)
    `arc_species_list`     ``list``   A list of ARCSpecies objects (each entry represent either a stable well
                                        or a TS)
    'rxn_list'             ``list``   A list of RMG Reaction objects. Will (hopefully) be converted into TSs
    'conformer_level'      ``str``    Level of theory for conformer searches
    'composite_method'     ``str``    Composite method
    'opt_level'            ``str``    Level of theory for geometry optimization
    'freq_level'           ``str``    Level of theory for frequency calculations
    'sp_level'             ``str``    Level of theory for single point calculations
    'scan_level'           ``str``    Level of theory for rotor scans
    'output'               ``dict``   Output dictionary with status and final QM files for all species
    'fine'                 ``bool``   Whether or not to use a fine grid for opt jobs (spawns an additional job)
    'generate_conformers'  ``bool``   Whether or not to generate conformers when an initial geometry is given
    'scan_rotors'          ``bool``   Whether or not to perform rotor scans
    'use_bac'              ``bool``   Whether or not to use bond additivity corrections for thermo calculations
    'model_chemistry'      ``list``   The model chemistry in Arkane for energy corrections (AE, BAC).
                                        This can be usually determined automatically.
    ====================== ========== =========================================================================

    `level_of_theory` is a string representing either sp//geometry levels or a composite method, e.g. 'CBS-QB3',
                                                 'CCSD(T)-F12a/aug-cc-pVTZ//B3LYP/6-311++G(3df,3pd)'...
    """
    def __init__(self, project, rmg_species_list=list(), arc_species_list=list(), rxn_list=list(),
                 level_of_theory='', conformer_level='', composite_method='', opt_level='', freq_level='', sp_level='',
                 scan_level='', fine=True, generate_conformers=True, scan_rotors=True, use_bac=True,
                 model_chemistry='', verbose=logging.INFO):

        self.project = project
        self.t0 = time.time()  # init time
        self.output_directory = os.path.join(arc_path, 'Projects', self.project)
        if not os.path.exists(self.output_directory):
            os.makedirs(self.output_directory)
        self.fine = fine
        self.generate_conformers = generate_conformers
        self.scan_rotors = scan_rotors
        self.use_bac = use_bac
        self.model_chemistry = model_chemistry
        if self.model_chemistry:
            logging.info('Using {0} as model chemistry for energy corrections in Arkane'.format(self.model_chemistry))
        if not self.fine:
            logging.info('\n')
            logging.warning('Not using a fine grid for geometry optimization jobs')
            logging.info('\n')
        if not self.scan_rotors:
            logging.info('\n')
            logging.warning("Not running rotor scans."
                            " This might compromise geometry as dihedral angles won't be corrected")
            logging.info('\n')
        self.output = dict()
        self.verbose = verbose
        self.initialize_log(verbose=self.verbose, log_file=os.path.join(self.output_directory, 'arc.log'))

        logging.info('Starting project {0}\n\n'.format(self.project))

        if level_of_theory.count('//') > 1:
            raise InputError('Level of theory seems wrong. It should either be a composite method (like CBS-QB3)'
                             ' or be of the form sp//geometry, e.g., CCSD(T)-F12/avtz//wB97x-D3/6-311++g**.'
                             ' Got: {0}'.format(level_of_theory))

        if conformer_level:
            logging.info('Using {0} for refined conformer searches (after filtering via force fields)'.format(
                conformer_level))
            self.conformer_level = conformer_level.lower()
        elif self.generate_conformers:
            self.conformer_level = default_levels_of_theory['conformer'].lower()
            logging.info('Using default level {0} for refined conformer searches (after filtering via force'
                         ' fields)'.format(default_levels_of_theory['conformer']))
        else:
            self.conformer_level = ''

        if level_of_theory:
            if '/' not in level_of_theory:  # assume this is a composite method
                self.composite_method = level_of_theory.lower()
                logging.info('Using composite method {0}'.format(self.composite_method))
            elif '//' in level_of_theory:
                self.opt_level = level_of_theory.lower().split('//')[1]
                self.freq_level = level_of_theory.lower().split('//')[1]
                self.sp_level = level_of_theory.lower().split('//')[0]
                logging.info('Using {0} for geometry optimizations'.format(level_of_theory.split('//')[1]))
                logging.info('Using {0} for frequency calculations'.format(level_of_theory.split('//')[1]))
                logging.info('Using {0} for single point calculations'.format(level_of_theory.split('//')[0]))
            elif '/' in level_of_theory and '//' not in level_of_theory:
                # assume this is not a composite method, and the user meant to run opt, freq and sp at this level.
                # running an sp after opt at the same level is meaningless, but doesn't matter much also
                # The '//' combination will later assist in differentiating between composite to non-composite methods
                self.opt_level = level_of_theory.lower()
                self.freq_level = level_of_theory.lower()
                self.sp_level = level_of_theory.lower()
                logging.info('Using {0} for geometry optimizations'.format(level_of_theory))
                logging.info('Using {0} for frequency calculations'.format(level_of_theory))
                logging.info('Using {0} for single point calculations'.format(level_of_theory))
        else:
            self.composite_method = composite_method.lower()
            if self.composite_method:
                if level_of_theory and level_of_theory.lower != self.composite_method:
                    raise InputError('Specify either composite_method or level_of_theory')
                logging.info('Using composite method {0}'.format(composite_method))
                if self.composite_method == 'cbs-qb3':
                    self.model_chemistry = self.composite_method
                    logging.info('Using {0} as model chemistry for energy corrections in Arkane'.format(
                        self.model_chemistry))
                elif self.use_bac:
                    raise InputError('Could not determine model chemistry to use for composite method {0}'.format(
                        self.composite_method))

            if opt_level:
                self.opt_level = opt_level.lower()
                logging.info('Using {0} for geometry optimizations'.format(self.opt_level))
            elif not self.composite_method:
                # self.opt_level = 'wb97x-d3/def2-tzvpd'
                # logging.info('Using wB97x-D3/def2-TZVPD for geometry optimizations')
                self.opt_level = default_levels_of_theory['opt'].lower()
                logging.info('Using default level {0} for geometry optimizations'.format(self.opt_level))
            else:
                self.opt_level = ''

            if freq_level:
                self.freq_level = freq_level.lower()
                logging.info('Using {0} for frequency calculations'.format(self.freq_level))
            elif not self.composite_method:
                if opt_level:
                    self.freq_level = opt_level.lower()
                    logging.info('Using user-defined opt level {0} for frequency calculations as well'.format(
                        self.freq_level))
                else:
                    # self.freq_level = 'wb97x-d3/def2-tzvpd'
                    # logging.info('Using wB97x-D3/def2-TZVPD for frequency calculations')
                    self.freq_level = default_levels_of_theory['freq'].lower()
                    logging.info('Using default level {0} for frequency calculations'.format(self.freq_level))
            else:
                self.freq_level = default_levels_of_theory['freq_for_composite'].lower()
                logging.info('Using default level {0} for frequency calculations after composite jobs'.format(
                    self.freq_level))

            if sp_level:
                self.sp_level = sp_level.lower()
                logging.info('Using {0} for single point calculations'.format(self.sp_level))
                self.check_model_chemistry()
            elif not self.composite_method:
                self.sp_level = default_levels_of_theory['sp'].lower()
                logging.info('Using default level {0} for single point calculations'.format(self.sp_level))
                self.check_model_chemistry()
            else:
                self.sp_level = ''

        if scan_level:
            self.scan_level = scan_level.lower()
            logging.info('Using {0} for rotor scans'.format(self.scan_level))
        elif self.scan_rotors:
            self.scan_level = default_levels_of_theory['scan'].lower()
            logging.info('Using default level {0} for rotor scans'.format(self.scan_level))
        else:
            self.scan_level = ''

        self.arc_species_list = []
        self.arc_species_list.extend(arc_species_list)
        self.rmg_species_list = rmg_species_list
        if self.rmg_species_list:
            for rmg_spc in self.rmg_species_list:
                if not isinstance(rmg_spc, Species):
                    raise InputError('All entries of rmg_species_list have to be RMG Species objects.'
                                     ' Got: {0}'.format(type(rmg_spc)))
                if not rmg_spc.label:
                    raise InputError('Missing label on RMG Species object {0}'.format(rmg_spc))
                arc_spc = ARCSpecies(is_ts=False, rmg_species=rmg_spc)  # assuming an RMG Species is not a TS
                self.arc_species_list.append(arc_spc)

        self.rxn_list = rxn_list

        self.scheduler = None

    def execute(self):
        logging.info('\n\n')
        for species in self.arc_species_list:
            if not isinstance(species, ARCSpecies):
                raise ValueError('All species in species_list must be ARCSpecies objects.'
                                 ' Got {0}'.format(type(species)))
            logging.info('Considering species: {0}'.format(species.label))
        logging.info('\n')
        for rxn in self.rxn_list:
            if not isinstance(rxn, Reaction):
                logging.error('`rxn_list` must be a list of RMG.Reaction objects. Got {0}'.format(type(rxn)))
                raise ValueError()
            logging.info('Considering reacrion {0}'.format(rxn))
        logging.info('\n')
        self.scheduler = Scheduler(project=self.project, species_list=self.arc_species_list,
                                   composite_method=self.composite_method, conformer_level=self.conformer_level,
                                   opt_level=self.opt_level, freq_level=self.freq_level, sp_level=self.sp_level,
                                   scan_level=self.scan_level, fine=self.fine,
                                   generate_conformers=self.generate_conformers, scan_rotors=self.scan_rotors)
        prc = Processor(project=self.project, species_dict=self.scheduler.species_dict, output=self.scheduler.output,
                        use_bac=self.use_bac, model_chemistry=self.model_chemistry)
        prc.process()
        self.summary()
        self.log_footer()

    def summary(self):
        """
        Report status and data of all species / reactions
        """
        logging.info('\n\n\nAll jobs terminated. Project summary:\n')
        for label, output in self.scheduler.output.iteritems():
            if output['status'] == 'converged':
                logging.info('Species {0} converged successfully'.format(label))
            else:
                logging.info('Species {0} failed with message:\n  {1}'.format(label, output['status']))

    def initialize_log(self, verbose=logging.INFO, log_file=None):
        """
        Set up a logger for ARC to use to print output to stdout.
        The `verbose` parameter is an integer specifying the amount of log text seen
        at the console; the levels correspond to those of the :data:`logging` module.
        """
        # Create logger
        logger = logging.getLogger()
        # logger.setLevel(verbose)
        logger.setLevel(logging.DEBUG)

        # Use custom level names for cleaner log output
        logging.addLevelName(logging.CRITICAL, 'Critical: ')
        logging.addLevelName(logging.ERROR, 'Error: ')
        logging.addLevelName(logging.WARNING, 'Warning: ')
        logging.addLevelName(logging.INFO, '')
        logging.addLevelName(logging.DEBUG, '')
        logging.addLevelName(0, '')

        # Create formatter and add to handlers
        formatter = logging.Formatter('%(levelname)s%(message)s')

        # Remove old handlers before adding ours
        while logger.handlers:
            logger.removeHandler(logger.handlers[0])

        # Create console handler; send everything to stdout rather than stderr
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(verbose)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

        # Create file handler
        if log_file:
            fh = logging.FileHandler(filename=log_file)
            fh.setLevel(min(logging.DEBUG,verbose))
            fh.setFormatter(formatter)
            logger.addHandler(fh)
            self.log_header()

    def log_header(self, level=logging.INFO):
        """
        Output a header containing identifying information about CanTherm to the log.
        """
        logging.log(level, 'ARC execution initiated at {0}'.format(time.asctime()))
        logging.log(level, '')
        logging.log(level, '###############################################################')
        logging.log(level, '#                                                             #')
        logging.log(level, '#                            ARC                              #')
        logging.log(level, '#                                                             #')
        logging.log(level, '#   Version: 0.1                                              #')
        logging.log(level, '#                                                             #')
        logging.log(level, '###############################################################')
        logging.log(level, '')

    def log_footer(self, level=logging.INFO):
        """
        Output a footer to the log.
        """
        logging.log(level, '')
        t = time.time() - self.t0
        m, s = divmod(t, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        if d > 0:
            d = str(d) + ' days, '
        else:
            d = ''
        logging.log(level, 'Total execution time: {0}{1:02.0f}:{2:02.0f}:{3:02.0f}'.format(d,h,m,s))
        logging.log(level, 'ARC execution terminated at {0}'.format(time.asctime()))

    def check_model_chemistry(self):
        if self.model_chemistry:
            self.model_chemistry = self.model_chemistry.lower()
            logging.info('Using {0} as model chemistry for energy corrections in Arkane'.format(
                self.model_chemistry))
            if self.model_chemistry not in ['cbs-qb3', 'cbs-qb3-paraskevas', 'ccsd(t)-f12/cc-pvdz-f12',
                                            'ccsd(t)-f12/cc-pvtz-f12', 'ccsd(t)-f12/cc-pvqz-f12',
                                            'b3lyp/cbsb7', 'b3lyp/6-311g(2d,d,p)', 'b3lyp/6-311+g(3df,2p)',
                                            'b3lyp/6-31g**']:
                logging.warn('No bond additivity corrections (BAC) are available in Arkane for "model chemistry"'
                             ' {0}. As a result, thermodynamic parameters are expected to be inaccurate. Make sure that'
                             ' atom energy corrections (AEC) were supplied or are available in Arkane to avoid'
                             ' error.'.format(self.model_chemistry))
        else:
            # model chemistry was not given, try to determine it from the sp_level
            model_chemistry = ''
            sp_level = self.sp_level.lower()
            sp_level = sp_level.replace('f12a', 'f12').replace('f12b', 'f12')
            if sp_level in ['ccsd(t)-f12/cc-pvdz', 'ccsd(t)-f12/cc-pvtz', 'ccsd(t)-f12/cc-pvqz']:
                logging.warning('Using model chemistry {0} based on sp level {1}.'.format(
                    sp_level + '-f12', sp_level))
                model_chemistry = sp_level + '-f12'
            elif not model_chemistry and sp_level in ['cbs-qb3', 'cbs-qb3-paraskevas', 'ccsd(t)-f12/cc-pvdz-f12',
                                                      'ccsd(t)-f12/cc-pvtz-f12', 'ccsd(t)-f12/cc-pvqz-f12',
                                                      'b3lyp/cbsb7', 'b3lyp/6-311g(2d,d,p)', 'b3lyp/6-311+g(3df,2p)',
                                                      'b3lyp/6-31g**']:
                model_chemistry = sp_level
            elif self.use_bac:
                raise InputError('Could not determine appropriate model chemistry to be used in Arkane for'
                                 ' thermochemical parameter calculations. Either turn off the "use_bac" flag'
                                 ' (and BAC will not be used), or specify a correct model chemistry. For a'
                                 ' comprehensive model chemistry list allowed in Arkane, see the Arkane documentation'
                                 ' on the RMG website, rmg.mit.edu.')
            else:
                # use_bac is False, and no model chemistry was specified
                if sp_level in ['m06-2x/cc-pvtz', 'g3', 'm08so/mg3s*', 'klip_1', 'klip_2', 'klip_3', 'klip_2_cc',
                                'ccsd(t)-f12/cc-pvdz-f12_h-tz', 'ccsd(t)-f12/cc-pvdz-f12_h-qz',
                                'ccsd(t)-f12/cc-pvdz-f12', 'ccsd(t)-f12/cc-pvtz-f12', 'ccsd(t)-f12/cc-pvqz-f12',
                                'ccsd(t)-f12/cc-pcvdz-f12', 'ccsd(t)-f12/cc-pcvtz-f12', 'ccsd(t)-f12/cc-pcvqz-f12',
                                'ccsd(t)-f12/cc-pvtz-f12(-pp)', 'ccsd(t)/aug-cc-pvtz(-pp)', 'ccsd(t)-f12/aug-cc-pvdz',
                                'ccsd(t)-f12/aug-cc-pvtz', 'ccsd(t)-f12/aug-cc-pvqz', 'b-ccsd(t)-f12/cc-pvdz-f12',
                                'b-ccsd(t)-f12/cc-pvtz-f12', 'b-ccsd(t)-f12/cc-pvqz-f12', 'b-ccsd(t)-f12/cc-pcvdz-f12',
                                'b-ccsd(t)-f12/cc-pcvtz-f12', 'b-ccsd(t)-f12/cc-pcvqz-f12', 'b-ccsd(t)-f12/aug-cc-pvdz',
                                'b-ccsd(t)-f12/aug-cc-pvtz', 'b-ccsd(t)-f12/aug-cc-pvqz', 'mp2_rmp2_pvdz',
                                'mp2_rmp2_pvtz', 'mp2_rmp2_pvqz', 'ccsd-f12/cc-pvdz-f12',
                                'ccsd(t)-f12/cc-pvdz-f12_noscale', 'g03_pbepbe_6-311++g_d_p', 'fci/cc-pvdz',
                                'fci/cc-pvtz', 'fci/cc-pvqz','bmk/cbsb7', 'bmk/6-311g(2d,d,p)', 'b3lyp/6-31g**',
                                'b3lyp/6-311+g(3df,2p)', 'MRCI+Davidson/aug-cc-pV(T+d)Z']:
                    model_chemistry = sp_level
            self.model_chemistry = model_chemistry
            logging.info('Using {0} as model chemistry for energy corrections in Arkane'.format(
                self.model_chemistry))


def delete_all_arc_jobs(server_name):
    """
    Delete all ARC-spawned jobs (with job name starting with `a` and a digit) from server `server_name`
    Make sure you know what you're doing, so unrelated jobs won't be deleted...
    Useful when terminating ARC while some (ghost) jobs are still running.
    """
    logging.info('Deleting all ARC jobs from {0}'.format(server_name))
    cmd = check_status_command[servers[server_name]['cluster_soft']] + ' -u ' + servers[server_name]['un']
    ssh = SSH_Client(server_name)
    stdout, stderr = ssh.send_command_to_server(cmd)
    for status_line in stdout:
        if re.match(' a\d', status_line):
            job_id = re.search(' a\d+', status_line)
            ssh.delete_job(job_id)
            logging.info('deleted job {0}'.format(job_id))


# TODO: sucsessive opt (B3LYP, CCSD, CISD(T), MRCI)
# TODO: need to know optical isomers and external symmetry (could also be read from QM, but not always right) for thermo
# TODO: calc thermo and rates
# TODO: mongodb?  https://github.com/PACChem/QTC/blob/master/qtc/dbtools.py
# TODO: make visuallization files
# TODO: MRCI input file and auto-occ/closed/frozed...
# TODO: eventually log all levels of theory used for a species. Could be in YAML
# TODO: make it run on the server
# TODO: what if a species has an imaginary freq? wait for rotor results, it could improve via the dihedral correction. But if not?
# TODO: solve the problem w/ molpro running from ARC
# TODO: find where to chack status and call  job.troubleshoot_server()
# TODO: submit jobs in a job list (Colin)
# TODO: Py3 proof (__future__)


