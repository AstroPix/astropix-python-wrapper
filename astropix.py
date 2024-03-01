"""
Central module of astropix. This incorporates all of the various modules from the original 'module' directory backend (now 'core')
The class methods of all the other modules/cores are inherited here. 

Author: Autumn Bauman
Maintained by: Amanda Steinhebel, amanda.l.steinhebel@nasa.gov
"""
# Needed modules. They all import their own suppourt libraries, 
# and eventually there will be a list of which ones are needed to run
from typing import Dict
from core.spi import Spi 
from core.nexysio import Nexysio
from core.decode import Decode
from core.injectionboard import Injectionboard
from core.voltageboard import Voltageboard
from core.asic import Asic
from bitstring import BitArray
from tqdm import tqdm
import pandas as pd
import time
import yaml
import os, sys

# Logging stuff
import logging
from modules.setup_logger import logger
logger = logging.getLogger(__name__)

class astropixRun:

    # Init just opens the chip and gets the handle. After this runs
    # asic_config also needs to be called to set it up. Seperating these 
    # allows for simpler specifying of values. 
    def __init__(self, chipversion=2, inject:int = None, offline:bool=False):
        """
        Initalizes astropix object. 
        No required arguments
        Optional:
        inject:bool - if set to True will enable injection for the whole array.
        offline:bool - if True, do not try to interface with chip
        """

        # _asic_start tracks if the inital configuration has been run on the ASIC yet.
        # By not handeling this in the init it simplifies the function, making it simpler
        # to put in custom configurations and allows for less writing to the chip,
        # only doing it once at init or when settings need to be changed as opposed to 
        # each time a parameter is changed.

        if offline:
            logger.info("Creating object for offline analysis")
        else:
            self._asic_start = False
            self.nexys = Nexysio()
            self._wait_progress(2)
            self.handle = self.nexys.autoopen() 
                
            # Ensure it is working
            logger.info("Opened FPGA, testing...")
            self._test_io()
            logger.info("FPGA test successful.")
            # Start putting the variables in for use down the line
            if inject is None:
                inject = (None, None)
            self.injection_col = inject[1]
            self.injection_row = inject[0]

        self.chipversion = chipversion

##################### YAML INTERACTIONS #########################
#reading done in core/asic.py
#writing done here

    def write_conf_to_yaml(self, filename:str = None):
        """
        Write ASIC config to yaml
        :param chipversion: chip version
        :param filename: Name of yml file in config folder
        """

        dicttofile ={self.asic.chip:
            {
                "telescope": {"nchips": self.asic.num_chips},
                "geometry": {"cols": self.asic.num_cols, "rows": self.asic.num_rows}
            }
        }

        if self.asic.num_chips > 1:
            for chip in range(self.asic.num_chips):
                dicttofile[self.asic.chip][f'config_{chip}'] = self.asic.asic_config[f'config_{chip}']
        else:
            dicttofile[self.asic.chip]['config'] = self.asic.asic_config

        with open(f"{filename}", "w", encoding="utf-8") as stream:
            try:
                yaml.dump(dicttofile, stream, default_flow_style=False, sort_keys=False)

            except yaml.YAMLError as exc:
                logger.error(exc)
                sys.exit(1)
        

##################### ASIC METHODS FOR USERS #########################

    # Method to initalize the asic. This is taking the place of asic.py. 
    # All of the interfacing is handeled through asic_update
    def asic_init(self, yaml:str = None, dac_setup: dict = None, bias_setup:dict = None, blankmask:bool = False, analog_col:int = None):
        """
        self.asic_init() - initalize the asic configuration. Must be called first
        Positional arguments: None
        Optional:
        dac_setup: dict - dictionary of values passed to the configuration, voltage OR current DAC. Only needs values diffent from defaults        
        bias_setup: dict - dict of values for the bias configuration Only needs key/vals for changes from default
        blankmask: bool - Create a blank mask (everything disabled). Pixels can be enabled manually 
        analog_col: int - Sets a column to readout analog data from. 
        """

        # Now that the asic has been initalized we can go and make this true
        self._asic_start = True

        self.asic = Asic(self.handle, self.nexys)

        #Define YAML path variables
        pathdelim=os.path.sep #determine if Mac or Windows separators in path name
        ymlpath="."+pathdelim+"config"+pathdelim+yaml+".yml"

        #Get config values from YAML and set chip properties
        try:
            self.asic.load_conf_from_yaml(self.chipversion, ymlpath)
        except Exception:
            logger.error('Must pass a configuration file in the form of *.yml - check the path/file name')
            sys.exit(1)
        #Config stored in dictionary self.asic_config . This is used for configuration in asic_update. 
        #If any changes are made, make change to self.asic_config so that it is reflected on-chip when 
        # asic_update is called

        #Sort DAC settings to idac vs vdac
        idac_setup, vdac_setup = None, None
        if dac_setup:
            for k in dac_setup.keys():
                if k in self.asic.asic_config['idacs']:
                    idac_setup = {k: dac_setup[k]}
                elif k in self.asic.asic_config['vdacs']:
                    vdac_setup = {k: dac_setup[k]}
                else:
                    logger.warning(f"Sent bad DAC value {dac_setup} - not a DAC setting. Aborting DAC update")
                    return

        #Override yaml if arguments were given in run script
        self.update_asic_config(bias_setup, idac_setup, vdac_setup)

        # Set analog output
        if (analog_col is not None) and (analog_col <= self.asic._num_cols):
            logger.info(f"enabling analog output in column {analog_col}")
            self.asic.enable_ampout_col(analog_col, inplace=False)

        # Turns on injection if so desired 
        if self.injection_col is not None:
            self.asic.set_inj_col(self.injection_col, True)
            self.asic.set_inj_row(self.injection_row, True)

        # Load config it to the chip
        logger.info("LOADING TO ASIC...")
        self.asic_update()
        logger.info("ASIC SUCCESSFULLY CONFIGURED")

    #Interface with asic.py 
    def enable_pixel(self, col: int, row: int):
       self.asic.set_pixel_comparator(col, row, True)

    def disable_pixel(self, col: int, row: int):
       self.asic.set_pixel_comparator(col, row, False)

    #Turn on injection of different pixel than the one used in _init_
    def enable_injection(self, col:int, row:int):
        self.asic.set_inj_col(col, True)
        self.asic.set_inj_row(row, True)

    # The method to write data to the asic. Called whenever somthing is changed
    # or after a group of changes are done. Taken straight from asic.py.
    def asic_update(self):
        self.nexys.chip_reset()        
        self.asic.asic_update()


    # Methods to update the internal variables. Please don't do it manually
    # This updates the dac config
    def update_asic_config(self, bias_cfg:dict = None, idac_cfg:dict = None, vdac_cfg:dict = None):
        """
        Updates and writes confgbits to asic

        bias_cfg:dict - Updates the bias settings. Only needs key/value pairs which need updated
        idac_cfg:dict - Updates iDAC settings. Only needs key/value pairs which need updated
        vdac_cfg:dict - Updates vDAC settings. Only needs key/value pairs which need updated
        """
        if self._asic_start:
            if bias_cfg is not None:
                for key in bias_cfg:
                    self.asic.asic_config['biasconfig'][key][1]=bias_cfg[key]
            if idac_cfg is not None:
                for key in idac_cfg:
                    self.asic.asic_config['idacs'][key][1]=idac_cfg[key]
            if vdac_cfg is not None:
                for key in vdac_cfg:
                    self.asic.asic_config['vdacs'][key][1]=vdac_cfg[key]
            else: 
                logger.info("update_asic_config() got no arguments, nothing to do.")
                return None
            self.asic_update()
        else: raise RuntimeError("Asic has not been initalized")

    def enable_spi(self):
        """
        Starts spi bus. 

        Takes no arguments, returns nothing
        """

        self.nexys.spi_enable()
        self.nexys.spi_reset_fpga_readout()
        # Set SPI clockdivider
        # freq = 100 MHz/spi_clkdiv
        self.nexys.spi_clkdiv = 255
        self.nexys.send_routing_cmd()
        logger.info("SPI ENABLED")

    def asic_configure(self):
        self.asic_update()

    def close_connection(self):
        """
        Terminates the spi bus.
        Takes no arguments. No returns.
        """
        self.nexys.close()


################## Voltageboard Methods ############################

# Here we intitalize the 8 DAC voltageboard in slot 4. 
    def init_voltages(self, slot: int = 4, vcal:float = .989, vsupply: float = 2.7, vthreshold:float = None, dacvals: tuple[int, list[float]] = None):
        """
        Configures the voltage board
        No required parameters. No return.

        slot:int = 4 - Position of voltage board
        vcal:float = 0.908 - Calibration of the voltage rails
        vsupply = 2.7 - Supply Voltage
        vthreshold:float = None - ToT threshold value. Takes precedence over dacvals if set. UNITS: mV
        dacvals:tuple[int, list[float] - vboard dac settings. Must be fully specified if set. 
        """
        # The default values to pass to the voltage dac. Last value in list is threshold voltage, default 100mV or 1.1
        # Included in YAML for v3 (not v2)

        # From nicholas's beam_test.py:
        # 1=thpmos (comparator threshold voltage), 3 = Vcasc2, 4=BL, 7=Vminuspix, 8=Thpix 
        if self.chipversion == 2:
            default_vdac = (8, [0, 0, 1.1, 1, 0, 0, 1, 1.100])
        else: #increase thpmos for v3 pmos pixels
            default_vdac = (8, [1.1, 0, 1.1, 1, 0, 0, 1, 1.100])

        # used to ensure this has been called in the right order:
        self._voltages_exist = True

        # Set dacvals
        if dacvals is None:
            dacvals = default_vdac
            # dacvals takes precidence over vthreshold
            if vthreshold is not None:
                # Turns from mV to V with the 1V offset normally present
                vthreshold = (vthreshold/1000) + default_vdac[1][3]
                if vthreshold > 1.5 or vthreshold < 0:
                    logger.warning("Threshold voltage out of range of sensor!")
                    if vthreshold <= 0: 
                        vthreshold = 1.100
                        logger.error("Threshold value too low, setting to default 100mV")
                dacvals[1][-1] = vthreshold
        # Create object
        self.vboard = Voltageboard(self.handle, slot, dacvals)
        # Set calibrated values
        self.vboard.vcal = vcal
        self.vboard.vsupply = vsupply
        # Send config to the chip
        self.vboard.update_vb()

    # Setup Injections
    def init_injection(self, slot: int = 3, inj_voltage:float = None, inj_period:int = 100, clkdiv:int = 300, initdelay: int = 100, cycle: float = 0, pulseperset: int = 1, dac_config:tuple[int, list[float]] = None, onchip: bool = False):
        """
        Configure injections
        No required arguments. No returns.
        Optional Arguments:
        slot: int - Location of the injection module
        inj_voltage: float - Injection Voltage. Range from 0 to 1.8. If dac_config is set inj_voltage will be overwritten
        inj_period: int
        clkdiv: int
        initdelay: int
        cycle: float
        pulseperset: int
        dac_config:tuple[int, list[float]]: injdac settings. Must be fully specified if set. 
        """
        
        """
        # Some fault tolerance
        try:
            self._voltages_exist
        except Exception:
            raise RuntimeError("init_voltages must be called before init_injection!")
        """

        # Fault tolerance 
        if (inj_voltage is not None) and (dac_config is None):
            # elifs check to ensure we are not injecting a negative value because we don't have that ability
            if inj_voltage < 0:
                raise ValueError("Cannot inject a negative voltage!")
            elif inj_voltage > 1800:
                logger.warning("Cannot inject more than 1800mV, will use defaults")
                inj_voltage = 300 #Sets to 300 mV
            self.asic.set_internal_vdac('vinj', inj_voltage/1000.)

        # Create injector object
        self.injector = Injectionboard(self.handle, self.asic, pos=3, onchip=onchip)

        if not onchip: #set voltageboard values if using GECCO card
            self.injector.vcal = self.vboard.vcal
            self.injector.vsupply = self.vboard.vsupply
            self.injector.amplitude = inj_voltage / 1000. #convert mV to V
            
        # Configure injector object
        self.injector.period = inj_period
        self.injector.clkdiv = clkdiv
        self.injector.initdelay = initdelay
        self.injector.cycle = cycle
        self.injector.pulsesperset = pulseperset       

    # These start and stop injecting voltage. Fairly simple.
    def start_injection(self):
        """
        Starts Injection.
        Takes no arguments and no return
        """
        self.injector.start()
        logger.info("Began injection")

    def stop_injection(self):
        """
        Stops Injection.
        Takes no arguments and no return
        """
        self.injector.stop()
        logger.info("Stopped injection")


########################### Input and Output #############################
    # This method checks the chip to see if a hit has been logged

    def hits_present(self):
        """
        Looks at interrupt
        Returns bool, True if present
        """
        if (int.from_bytes(self.nexys.read_register(70),"big") == 0):
            return True
        else:
            return False

    def get_log_header(self):
        """
        Returns header for use in a log file with all settings.
        """
        #Get config dictionaries from yaml
        vdac_str=""
        digitalconfig = {}
        for key in self.asic.asic_config['digitalconfig']:
                digitalconfig[key]=self.asic.asic_config['digitalconfig'][key][1]
        biasconfig = {}
        for key in self.asic.asic_config['biasconfig']:
                biasconfig[key]=self.asic.asic_config['biasconfig'][key][1]
        idacconfig = {}
        for key in self.asic.asic_config['idacs']:
                idacconfig[key]=self.asic.asic_config['idacs'][key][1]
        if self.chipversion>2:
            vdacconfig = {}
            for key in self.asic.asic_config['vdacs']:
                    vdacconfig[key]=self.asic.asic_config['vdacs'][key][1]
            vdac_str=f"vDAC: {vdacconfig}\n"
        arrayconfig = {}
        for key in self.asic.asic_config['recconfig']:
                arrayconfig[key]=self.asic.asic_config['recconfig'][key][1]

        # This is not a nice line, but its the most efficent way to get all the values in the same place.
        return f"Digital: {digitalconfig}\n" +f"Biasblock: {biasconfig}\n" + f"iDAC: {idacconfig}\n"+ f"Receiver: {arrayconfig}\n " + vdac_str



############################ Decoder ##############################
    # This function generates a list of the hits in the stream. Retuerns a bytearray

    def get_readout(self):
        """
        Reads hit buffer once triggered by chip 
        Returns bytearray
        """
        readout = self.nexys.read_spi_fifo()
        return readout

    def get_SW_readout(self, bufferlength:int = 20):
        """
        Reads hit buffer after pinging interupt 
        bufferlength:int - length of buffer to write. Multiplied by 8 to give number of bytes
        Returns bytearray
        """
        self.nexys.write_spi_bytes(bufferlength)
        readout = self.nexys.read_spi_fifo()
        return readout


    def decode_readout(self, readout:bytearray, i:int, clock_period_ns:int =5, printer: bool = True):
        """
        Decodes readout

        Required argument:
        readout: Bytearray - readout from sensor, not the printed Hex values
        i: int - Readout number

        Optional:
        clock_period_ns:int - period of main clock in ns
        printer: bool - Print decoded output to terminal

        Returns dataframe
        """
        # Creates object
        self.decode = Decode(clock_period_ns, nchips=self.asic.num_chips)

        list_hits = self.decode.hits_from_readoutstream(readout)
        df=self.decode.decode_astropix3_hits(list_hits, i, printer)

        return df

    # To be called when initalizing the asic, clears the FPGAs memory 
    def dump_fpga(self):
        """
        Force reads out hit buffer and disposes of the output.

        Does not return or take arguments. 
        """
        readout = self.get_readout()
        del readout


###################### INTERNAL METHODS ###########################

# Below here are internal methods used for constructing things and testing

    # _test_io(): A function to read and write a register on the chip to see if 
    # everything is working. 
    # It takes no arguments 
    def _test_io(self):
        try:    # Attempts to write to and read from a register
            self.nexys.write_register(0x09, 0x55, True)
            self.nexys.read_register(0x09)
            self.nexys.spi_reset_fpga_readout()
            self.nexys.sr_readback_reset()
        except Exception: 
            raise RuntimeError("Could not read or write from astropix!")

    # progress bar 
    def _wait_progress(self, seconds:int):
        for _ in tqdm(range(seconds), desc=f'Wait {seconds} s'):
            time.sleep(1)
