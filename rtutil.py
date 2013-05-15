
import datetime
import time
import os
import sys
import threading
import Queue as queue
import glob
import numpy as np
import dicom
import nibabel as nib
from nibabel.nicom import dicomreaders as dread
from nibabel.nicom import dicomwrappers as dwrap
import nipy.algorithms.registration

def findrecentdir(start_dir):
    all_dirs = [os.path.join(start_dir,d) for d in os.listdir(start_dir) if os.path.isdir(os.path.join(start_dir,d))]
    if all_dirs:
        last_mod = max((os.path.getmtime(d),d) for d in all_dirs)[1]
        return last_mod
    else:
        return False

def navigatedown(start_dir):
    current_dir = start_dir
    bottom = False
    while not bottom:
        sub_dir = findrecentdir(current_dir)
        if not sub_dir:
            bottom = True
        else:
            current_dir = sub_dir
    return current_dir

def get_current(top_dir):
    most_recent_dir = navigatedown(top_dir)
    current_dir = os.path.abspath(os.path.join(most_recent_dir, '../'))
    all_dirs = [d for d in os.listdir(current_dir) if os.path.isdir(os.path.join(current_dir,d))]
    return [current_dir,all_dirs]

def wait_for_new_directory(root_dir, black_list, waittime):
    baseT = time.time()
    while (time.time() - baseT) < waittime:
        recent_dir = findrecentdir(root_dir)
        if not os.path.basename(recent_dir) in black_list:
            return recent_dir
        time.sleep(0.5)
    return False


class IncrementalDicomFinder(threading.Thread):
    """
    Find new DICOM files in the series_path directory and put them into the dicom_queue.
    """
    def __init__(self, rtclient, series_path, dicom_queue, interval):
        super(IncrementalDicomFinder, self).__init__()
        self.rtclient = rtclient
        self.series_path = series_path
        self.dicom_queue = dicom_queue
        self.interval = interval
        self.alive = True
        self.server_inum = 0
        self.dicom_nums = []
        self.dicom_search_start = 0
        self.exam_num = None
        self.series_num = None
        self.acq_num = None
        print 'initialized'

    def halt(self):
        self.alive = False

    def get_initial_filelist(self):
        time.sleep(0.1)
        files = self.rtclient.get_file_list(self.series_path)
        files.sort()
        if files:
            for fn in files:
                spl = os.path.basename(fn).split('.')
                current_inum = int(spl[0][1:])
                if current_inum > self.server_inum:
                    self.server_inum = current_inum
                self.dicom_nums.append(int(spl[2]))
            gaps = [x for x in range(max(self.dicom_nums)) if x not in self.dicom_nums]
            gaps.remove(0)
            if gaps:
                self.dicom_search_start = min(gaps)
            else:
                self.dicom_search_start = max(self.dicom_nums)+1
            return files
        else:
            return False

    def check_dcm(self, dcm, verbose=True):
        if not self.exam_num:
            self.exam_num = int(dcm.StudyID)
            self.series_num = int(dcm.SeriesNumber)
            self.acq_num = int(dcm.AcquisitionNumber)
            self.series_description = dcm.SeriesDescription
            self.patient_id = dcm.PatientID
            if verbose:
                print('Acquiring dicoms for exam %d, series %d (%s / %s)'
                        % (self.exam_num, self.series_num, self.patient_id, self.series_description))
        if self.exam_num != int(dcm.StudyID) or self.series_num != int(dcm.SeriesNumber):
            if verbose:
                print('Skipping dicom because of exam/series mis-match (%d, %d).' % (self.exam_num, self.series_num))
            return False
        else:
            return True


    def run(self):
        take_a_break = False
        failures = 0

        while self.alive:
            #print sorted(self.dicom_nums)
            #print self.server_inum
            before_check = datetime.datetime.now()
            #print before_check

            if self.server_inum == 0:
                filenames = self.get_initial_filelist()
                for fn in filenames:
                    dcm = self.rtclient.get_dicom(fn)
                    if self.check_dcm(dcm):
                        self.dicom_queue.put(dcm)
            elif take_a_break:
                #print '%s: (%d) [%f]' % (os.path.basename(self.series_path), self.server_inum, self.interval)
                time.sleep(self.interval)
                take_a_break = False
            else:
                loop_success = False
                first_failure = False
                ind_tries = [x for x in range(self.dicom_search_start, max(self.dicom_nums)+10) if x not in self.dicom_nums]
                #print ind_tries
                for d in ind_tries:
                    try:
                        current_filename = 'i'+str(self.server_inum+1)+'.MRDC.'+str(d)
                        #print current_filename
                        dcm = self.rtclient.get_dicom(os.path.join(self.series_dir, current_filename))
                        if not len(dcm.PixelData) == 2 * dcm.Rows * dcm.Columns:
                            print 'corruption error'
                            print 'pixeldata: '+str(len(dcm.PixelData))
                            print 'expected: '+str(2*dcm.Rows*dcm.Columns)
                            raise Exception
                    except:
                        #print current_filename+', failed attempt'
                        if not first_failure:
                            self.dicom_search_start = d
                            first_failure = True
                    else:
                        #print current_filename+', successful attempt'+'\n'
                        if self.check_dcm(dcm):
                            self.dicom_queue.put(dcm)
                        self.dicom_nums.append(d)
                        self.server_inum += 1
                        loop_success = True
                        failures = 0

                if not loop_success:
                    #print 'failure on: i'+str(self.server_inum+1)+'\n'
                    refresher = glob.glob('i'+str(self.server_inum+1)+'*')
                    #failures = failures+1
                    take_a_break = True


class Volumizer(threading.Thread):
    """
    Volumizer converts dicom objects from the dicom queue into 3D volumes
    and pushes them onto the volume queue.
    """

    def __init__(self, dicom_q, volume_q, affine=None):
        super(Volumizer, self).__init__()
        self.dicom_q = dicom_q
        self.volume_q = volume_q
        self.alive = True
        self.affine = affine
        self.slices_per_volume = None
        self.completed_vols = 0
        self.vol_shape = None

    def halt(self):
        self.alive = False

    def run(self):
        dicoms = {}

        base_time = time.time()
        while self.alive:
            try:
                dcm = self.dicom_q.get(timeout=1)
            except queue.Empty:
                pass
            else:
                # convert incoming dicoms to 3D volumes
                if self.slices_per_volume is None:
                    TAG_SLICES_PER_VOLUME = (0x0021, 0x104f)
                    self.slices_per_volume = int(dcm[TAG_SLICES_PER_VOLUME].value) if TAG_SLICES_PER_VOLUME in dcm else int(getattr(dcm, 'ImagesInAcquisition', 0))
                dicom = dwrap.wrapper_from_data(dcm)
                if self.affine is None:
                    # FIXME: dicom.get_affine is broken for our GE files. We should fix that!
                    #self.affine = dicom.get_affine()
                    self.affine = np.eye(4)
                    mm_per_vox = [float(i) for i in dcm.PixelSpacing + [dcm.SpacingBetweenSlices]] if 'PixelSpacing' in dcm and 'SpacingBetweenSlices' in dcm else [0.0, 0.0, 0.0]
                    pos = tuple(dcm.ImagePositionPatient)
                    self.affine[0:3,0:3] = np.diag(mm_per_vox)
                    self.affine[:,3] = np.array((-pos[0], -pos[1], pos[2], 1)).T
                    print(self.affine)
                dicoms[dicom.instance_number] = dicom

                #print 'put in dicom:' + str(dicom.instance_number)

                # The dicoms instance number should indice where this dicom belongs.
                # It should start at 1 for the first slice of the first volume, and increment
                # by 1 for each subsequent slice/volume.
                start_inst = (self.completed_vols * self.slices_per_volume) + 1
                vol_inst_nums = range(start_inst, start_inst + self.slices_per_volume)
                # test to see if the dicom dict contains at least the dicom instance numbers that we need
                if all([(ind in dicoms) for ind in vol_inst_nums]):
                    cur_vol_shape = (dicoms[start_inst].image_shape[0], dicoms[start_inst].image_shape[1], self.slices_per_volume)
                    if not self.vol_shape:
                        self.vol_shape = cur_vol_shape
                    volume = np.zeros(self.vol_shape)
                    if self.vol_shape != cur_vol_shape:
                        print 'WARNING: Volume %03d is the wrong shape! Skipping...' % self.completed_vols
                    else:
                        for i,ind in enumerate(vol_inst_nums):
                            volume[:,:,i] = dicoms[ind].get_data()
                        volimg = nib.Nifti1Image(volume, self.affine)
                        self.volume_q.put(volimg)
                        print 'VOLUME %03d COMPLETE in %0.2f seconds!' % (self.completed_vols, time.time()-base_time)
                        #nib.save(volimg,'/tmp/rtmc_volume_%03d.nii.gz' % self.completed_vols)
                    self.completed_vols += 1
                    base_time = time.time()


class Analyzer(threading.Thread):
    """
    Analyzer gets 3D volumes out of the volume queue and computes real-time statistics on them.
    """

    #

    def __init__(self, volume_q, average_q, skip_vols=2):
        super(Analyzer, self).__init__()
        self.volume_q = volume_q
        self.average_q = average_q
        self.alive = True
        self.whole_brain = None
        self.brain_list = []
        self.ref_vol = None
        self.mean_img = 0.
        self.mc_xform = []
        self.mean_displacement = []
        self.max_displacement = 0.
        self.skip_vols = skip_vols

    def halt(self):
        # temp saver:
        #test_image = nib.Nifti1Image(self.ref_vol)
        #nib.save(test_image, '/tmp/rtmc_test_brain.nii')
        self.alive = False

    def run(self):
        vol_num = -1
        while self.alive:
            try:
                volimg = self.volume_q.get(timeout=1)
            except queue.Empty:
                pass
            else:
                vol_num += 1
                if vol_num>=self.skip_vols:
                    if not self.ref_vol:
                        print "SETTING REF VOL TO VOLUME #%03d" % vol_num
                        self.ref_vol = volimg
                    else:
                        # compute motion
                        #print "COMPUTING MOTION ON VOLUME #%03d" % vol_num
                        ref = self.ref_vol.get_data()
                        img = volimg.get_data()
                        # Ensure the arrays are 4d:
                        ref.shape += (1,) * (4 - ref.ndim)
                        img.shape += (1,) * (4 - img.ndim)
                        #print((ref.shape, img.shape))
                        # TODO: clean this up. We will be more efficient to use the lower-level routines
                        # like single_run_realign4d. Or write our own very simple alignment algorithm.
                        # BEGIN STDOUT SUPRESSION
                        actualstdout = sys.stdout
                        sys.stdout = open(os.devnull,'w')
                        im4d = nib.Nifti1Image(np.concatenate((ref, img), axis=3), self.ref_vol.get_affine())
                        reg = nipy.algorithms.registration.FmriRealign4d(im4d, 'ascending', time_interp=False)
                        reg.estimate(loops=2)
                        T = reg._transforms[0][1]
                        aligned_raw = reg.resample(0).get_data()[...,1]
                        sys.stdout = actualstdout
                        # END STDOUT SUPRESSION
                        #reg = nipy.algorithms.registration.HistogramRegistration(volimg, self.ref_vol)
                        #T = reg.optimize('rigid')
                        #aligned_raw = nipy.algorithms.registration.resample(volimg, T, self.ref_vol).get_data()
                        self.mean_img += aligned_raw.astype(float)
                        # get the full affine for this volume by pre-multiplying by the reference affine
                        mc_affine = np.dot(self.ref_vol.get_affine(), T.as_affine())
                        # Compute the error matrix
                        T_error = self.ref_vol.get_affine() - mc_affine
                        A = np.matrix(T_error[0:3,0:3])
                        t = np.matrix(T_error[0:3,3]).T
                        # radius of the spherical head assumption (in mm):
                        R = 70.
                        # The center of the volume. Assume 0,0,0 in world coordinates.
                        xc = np.matrix((0,0,0)).T
                        mean_disp = np.sqrt( R**2. / 5 * np.trace(A.T * A) + (t + A*xc).T * (t + A*xc) ).item()
                        self.mean_displacement.append(mean_disp)
                        if mean_disp > self.max_displacement:
                            self.max_displacement = mean_disp
                        self.mc_xform.append(T)
                        print "VOL %03d: mean displacement = %f mm, max displacement = %f mm" % (vol_num, mean_disp, self.max_displacement)


