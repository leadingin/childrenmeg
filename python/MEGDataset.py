import pickle
import numpy as np

from TrainingUtils import ThreadedBatcherizer
from model import TrainingComplete
# from functools import lru_cache
from cachetools import cached, RRCache
from abc import ABCMeta, abstractmethod
from threading import Lock, Thread, current_thread
from scipy.io import loadmat
from pathlib import Path


PREV_EVAL_FILE = 'preprocessed.pkl'

SUBJECT_TABLE = 'subject_table.mat'
SUBJECT_STRUCT = 'subject_struct.mat'

# Suffix in use
LOAD_SUFFIX = '.npy'

# Headers that we will import for the subjects
HEADER_ID = 'ID'
HEADER_AGE = 'age'
HEADER_SEX = 'sex'

# Test types
TEST_PDK = 'PDK'
TEST_PA = 'PA'
TEST_VG = 'VG'
TEST_MO = 'MO'

# todo make this less ugly
MEG_COLUMNS = Path('realcol.npy')


def zscore(l, nanprevention=1e-10):
    l = np.float32(l)
    return (l - np.mean(l, axis=0)) / (np.std(l, axis=0) + nanprevention)


def parsesubjects(subjecttable):
    """
    Given the path to the subject table, read and return a dictionary for the subjects
    :param subjecttable: String path, or Path object
    :return:
    """
    subject_dictionary = {}
    if isinstance(subjecttable, Path):
        struct = loadmat(str(subjecttable), squeeze_me=True)['subject_struct']
    elif isinstance(subjecttable, str):
        struct = loadmat(subjecttable, squeeze_me=True)['subject_struct']
    else:
        raise TypeError('Path to subject table not a recognized type')

    idlist = list(struct[HEADER_ID])
    agelist = list(struct[HEADER_AGE])
    sexlist = list(struct[HEADER_SEX])

    for subject in idlist:
        index = idlist.index(subject)
        subject_dictionary[subject] = \
            {HEADER_AGE: agelist[index], HEADER_SEX: sexlist[index]}

    return subject_dictionary


class CrossValidationComplete(Exception):
    pass


class BaseDataset(ThreadedBatcherizer):

    DATASET_TARGETS = [HEADER_AGE]
    NUM_BUCKETS = 10
    # cache = RRCache(1024)
    cache = RRCache(32768)

    # @lru_cache(maxsize=8192)
    @cached(cache)
    def get_flattened_features(self, path_to_file):
        """
        Loads arrays from file, and returned as a flattened vector, cached to save some time
        :param path_to_file:
        :return: numpy vector
        """
        # return loadmat(path_to_file, squeeze_me=True)['features'].ravel()
        l = np.load(path_to_file)

        if self.megind is not None:
            l = l[:, self.megind].squeeze()

        l = zscore(l)

        return l.ravel()

    @staticmethod
    def random_slices(dataset: np.ndarray, sizeofslices=(0.1, 0.9)):
        """
        Randomly segments the data into slices of size _sizeofslices_
        :param dataset: The dataset to perform this, splits the rows
        :param sizeofslices: tuple of size of slices, does not need to sum to 1
        :return: len(sizeofslices) list of ndarrays
        """
        to_return = []
        # Normalize to sum to 1
        sizeofslices = np.array(sizeofslices)/np.sum(sizeofslices)
        ind = np.arange(dataset.shape[0])
        for slice in sizeofslices:
            slice_ind = np.random.choice(ind, replace=False, size=int(slice * dataset.shape[0]))

            # Create masks to select the data
            mask = np.full(dataset.shape, False, bool)
            mask[slice_ind, :] = True
            to_return.append(dataset[mask].reshape((len(slice_ind), -1)))

        return to_return

    def __init__(self, toplevel, PDK=True, PA=True, VG=True, MO=False, batchsize=2):
        self.toplevel = Path(toplevel)
        # some basic checking to make sure we have the right directory
        if not self.toplevel.exists() or not self.toplevel.is_dir():
            raise NotADirectoryError("Provided top level directory is not directory")
        self.subject_hash = parsesubjects(self.toplevel / SUBJECT_STRUCT)

        self.batchsize = batchsize
        self.batchqueue = queue.Queue(100)
        self.varlock = Lock()
        self.loadevalthread = None
        self.itercount = 0
        self.alldataused = False
        self.leaveout = 0
        self.traindata = None
        self.evalx = None
        self.evaly = None
        self.testx = None
        self.testy = None
        tests = []
        # Assemble which experiments we are going to be using
        if PDK:
            tests.append(TEST_PDK)
        if PA:
            tests.append(TEST_PA)
        if VG:
            tests.append(TEST_VG)
        if MO:
            tests.append(TEST_MO)

        if self.preprocessed_file in [x.name for x in self.toplevel.iterdir() if not x.is_dir()]:
            with (self.toplevel / self.preprocessed_file).open('rb') as f:
                print('Loaded previous preprocessing!')
                self.buckets, self.longest_vector, self.slice_length,\
                self.testpoints, self.loaded_subjects = pickle.load(f)
                # list of subjects that we will use for the cross validation
                # self.leaveoutsubjects = np.unique(self.datapoints[:, 0])
                # Todo: warn/update pickled file if new subjects exist
        else:
            print('Preprocessing data...')

            self.loaded_subjects, self.longest_vector, self.slice_length = self.files_to_load(tests)

            testsubjects = np.random.choice(list(self.loaded_subjects.keys()),
                                            int(len(self.loaded_subjects)/10), replace=False)
            self.testpoints = np.array([item for x in testsubjects for item in self.loaded_subjects[x]])
            print('Subjects used for testing:', testsubjects)

            datapoint_ordering = sorted(self.loaded_subjects, key=lambda x: -len(self.loaded_subjects[x]))
            self.buckets = [[] for x in range(self.NUM_BUCKETS)]
            # Fill the buckets up and down
            for i in range(len(datapoint_ordering)):
                if int(i / self.NUM_BUCKETS) % 2:
                    index = self.NUM_BUCKETS - (i % self.NUM_BUCKETS) - 1
                    self.buckets[int(index)].extend(self.loaded_subjects[datapoint_ordering[i]])
                else:
                    self.buckets[int(i % self.NUM_BUCKETS)].extend(self.loaded_subjects[datapoint_ordering[i]])

            # self.datapoints = self.loaded_subjects[np.in1d(self.loaded_subjects[:, 0], self.leaveoutsubjects), :]

            # # leave out 10% of randomly selected data for test validation
            # self.testpoints, self.loaded_subjects = self.random_slices(self.datapoints, (0.1, 0.9))

            with (self.toplevel / self.preprocessed_file).open('wb') as f:
                pickle.dump((self.buckets, self.longest_vector, self.slice_length,
                             self.testpoints, self.loaded_subjects), f)
                # numpoints = self.datapoints.size[0]
                # ind = np.arange(numpoints)
                # ind = np.random.choice(ind, replace=False, size=int(0.2*numpoints)

        # Set first subject for crossvalidation
        # self.next_leaveout()

    @property
    @abstractmethod
    def modality_folders(self) -> list:
        """
        Subclasses must implement this so that it reports the name of the folder(s) to find experiments, once in the
        subject folder.
        :return:
        """
        pass

    def files_to_load(self, tests):
        """
        This should be implemented by subclasses to specify what files
        :param tests: The type of tests that should make up the dataset
        :return: A dictionary for the loaded subjects
        :rtype: tuple
        """
        longest_vector = -1
        slice_length = -1
        loaded_subjects = {}
        for subject in [x for x in self.toplevel.iterdir() if x.is_dir() and x.name in self.subject_hash.keys()]:
            print('Loading subject', subject.stem, '...')
            loaded_subjects[subject.stem] = []
            for experiment in [t for e in self.modality_folders if (subject / e).exists()
                               for t in (subject / e).iterdir() if t.name in tests]:

                for epoch in [l for l in experiment.iterdir() if l.suffix == LOAD_SUFFIX]:
                    try:
                        # f = loadmat(str(epoch), squeeze_me=True)
                        f = np.load(str(epoch))
                        # slice_length = max(slice_length, len(f['header']))
                        # longest_vector = max(longest_vector,
                        #                           len(f['features'].reshape(-1)))
                        slice_length = max(slice_length, f.shape[1])
                        longest_vector = max(longest_vector, f.shape[0]*f.shape[1])
                        loaded_subjects[subject.stem].append((subject.stem, epoch))
                    except Exception:
                        print('Warning: Skipping file, error occurred loading:', epoch)

        return loaded_subjects, longest_vector, slice_length

    @property
    @abstractmethod
    def preprocessed_file(self):
        pass

    def _load(self, batch: np.ndarray, cols: list):
        """
        Provided a batch of (subject, path_to_file), load the x and y vectors of data
        :param batch: The batch of filenames to load
        :return: (x_data, y_data)
        """
        x = np.zeros([batch.shape[0], self.longest_vector])
        y = np.zeros([batch.shape[0], 1])

        for row in range(x.shape[0]):
            ep = str(batch[row][1])
            temp = self.get_flattened_features(ep)
            x[row, :len(temp)] = temp
            y[row, :] = np.array([self.subject_hash[batch[row][0]][column] for column in cols])

        return x[~np.isnan(x).any(axis=1)], y[~np.isnan(x).any(axis=1)]

    def _set_eval(self, points):
        """
        Helper function that wraps the assignment of the eval variables so I can call with a different thread
        :param points:
        :return:
        """
        self.evalx, self.evaly = self._load(points, BaseDataset.DATASET_TARGETS)

    def next_leaveout(self, force=None):
        """
        Moves on to the next subject to leave out for cross validation.
        :return: The name of the subject, or None if complete
        """
        if force is not None:
            self.leaveout = force

        if self.leaveout == self.NUM_BUCKETS:
            print('Have completed cross-validation')
            raise CrossValidationComplete
            # return None

        # Select next bucket to leave out as evaluation
        self.eval_points = np.array(self.buckets[self.leaveout])

        # Convert the remaining buckets into one list
        self.traindata = np.array(
            [item for sublist in self.buckets for item in sublist if self.buckets.index(sublist) != self.leaveout]
        )

        # Load evaluation data in its own thread
        # self.loadevalthread = Thread(target=self._set_eval, args=[eval_points])
        # self.loadevalthread.start()
        print('Loading evaluation set')
        self._set_eval(self.eval_points)

        self.leaveout += 1

        return self.leaveout

    def __iter__(self):
        # Shuffle the order of the data we will train on
        np.random.shuffle(self.traindata)

        with self.varlock:
            self.itercount = self.batchsize
            self.alldataused = False

        # Immediately load a batch of 5, then shoot a thread to start loading a few more for later
        self._loadbatches(1)
        self.loadbatches()

        return self

    def __next__(self):
#        self._loadbatches(1)
        if self.batchqueue.empty():
            # Check if we went through all the data yet
            with self.varlock:
                if self.itercount == self.traindata.shape[0]:
                    self.alldataused = True
                    raise StopIteration
                elif self.alldataused:
                    raise StopIteration

            # Ensure more data is loaded
            print('Waiting on data...')
            self.loadbatches()
            for i in range(5):
                try:
                    return self.batchqueue.get(timeout=60)
                except (TimeoutError, queue.Empty):
                    print('Data loading very slowly...')
                    continue
            print("Can't wait anymore, I'm bored.")
            raise StopIteration
        else:
            try:
                self.loadbatches()
                return self.batchqueue.get(timeout=60)
            except (TimeoutError, queue.Empty):
                raise StopIteration

    def _loadbatches(self, numbatches=1):
        """
        Method that is run on new thread to load data from dataset
        :param numbatches: The number of batches to load with this method
        :return: Nothing, self.batchqueue is loaded with the data
        """
        if numbatches <= 0:
            numbatches = 1
        if self.alldataused:
            return

        for batch in range(numbatches):
            mybatch = 0
            with self.varlock:
                mybatch = self.itercount
                if self.itercount >= self.traindata.shape[0]:
                    mybatch = self.traindata.shape[0]
                    self.alldataused = True
                else:
                    self.itercount += self.batchsize

            # print('Thread ID:', current_thread(), 'Batch:', mybatch-self.batchsize, '/', self.traindata.shape[0])

            self.batchqueue.put(self._load(self.traindata[mybatch - self.batchsize:mybatch],
                                           BaseDataset.DATASET_TARGETS))

        # print('Thread ID:', current_thread(), 'Finished')


    # fixme: u g l y
    @property
    def leftout(self):
        return self.leaveout

    @property
    def testset(self):
        print('Loading Test Set... Dumping evaluation set from memory\n May take a while...')
        self.evalx = None
        self.evaly = None
        return self._load(self.testpoints, self.DATASET_TARGETS)

    @property
    def outputshape(self):
        return None, len(BaseDataset.DATASET_TARGETS)

    @property
    def evaluationset(self):
        if self.evalx is None:
            print('Reloading evaluation set...')
            self._set_eval(self.eval_points)

        return self.evalx, self.evaly
            # if self.loadevalthread is not None:
            #     self.loadevalthread.join()
            #     self.loadevalthread = None
            #     return self.evalx, self.evaly
            # elif self.evalx is not None:
            #     return self.evalx, self.evaly
            # else:
            #     raise NotImplementedError


    @property
    def inputshape(self):
        return None, self.longest_vector


class BaseDatasetAgeRanges(BaseDataset, metaclass=ABCMeta):

    # Age Ranges, lower inclusive, upper exclusive
    AGE_4_5 = (4, 6)
    AGE_6_7 = (6, 8)
    AGE_8_9 = (8, 10)
    AGE_10_11 = (10, 12)
    AGE_12_13 = (12, 14)
    AGE_14_15 = (14, 16)
    AGE_16_18 = (16, 19)
    AGE_RANGES = [AGE_4_5, AGE_6_7, AGE_8_9, AGE_10_11, AGE_12_13, AGE_14_15, AGE_16_18]

    def _load(self, batch: np.ndarray, cols: list):
        x, y_float = super()._load(batch, BaseDataset.DATASET_TARGETS)
        y = np.zeros([batch.shape[0], len(self.AGE_RANGES)])

        age_col = BaseDataset.DATASET_TARGETS.index(HEADER_AGE)
        for i in range(len(self.AGE_RANGES)):
            low = self.AGE_RANGES[i][0]
            high = self.AGE_RANGES[i][1]
            y[np.where((y_float[:, age_col] >= low) & (y_float[:, age_col] < high))[0], i] = 1

        return x[~np.isnan(x).any(axis=1)], y

    @property
    def outputshape(self):
        return None, len(self.AGE_RANGES)


# To make the MEG dataset, we ensure that the files that are loaded are from the MEG directory
class MEGDataset(BaseDataset):

    def __init__(self, toplevel, PDK=True, PA=True, VG=True, MO=False, batchsize=2):
        super().__init__(toplevel, PDK, PA, VG, MO, batchsize)
        if MEG_COLUMNS.exists():
            print('Found MEG Index')
            self.megind = np.load(str(MEG_COLUMNS))
        else:
            self.megind = None

    @property
    def modality_folders(self) -> list:
        return ['MEG']

    @property
    def preprocessed_file(self):
        return self.__class__.__name__ + PREV_EVAL_FILE


# Acoustic dataset is loaded by loading files from directory labelled Acoustic
class AcousticDataset(BaseDataset):

    @property
    def modality_folders(self) -> list:
        return ['Audio']

    @property
    def preprocessed_file(self):
        return self.__class__.__name__ + PREV_EVAL_FILE


class MEGAgeRangesDataset(MEGDataset, BaseDatasetAgeRanges):
    # Should work as is
    pass


class AcousticAgeRangeDataset(AcousticDataset, BaseDatasetAgeRanges):
    # Should work as is
    pass


class FusionDataset(MEGDataset, AcousticDataset):

    def __init__(self, toplevel, PDK=True, PA=True, VG=True, MO=False, batchsize=2):
        super().__init__(toplevel, PDK, PA, VG, MO, batchsize)
        if MEG_COLUMNS.exists():
            print('Found MEG Index')
            self.megind = np.load(str(MEG_COLUMNS))
        else:
            self.megind = None

    @cached(BaseDataset.cache)
    def get_flattened_features(self, path_to_file):
        """
        Loads arrays from file, and returned as a flattened vector, cached to save some time
        :param path_to_file:
        :return: numpy vector
        """
        # m = loadmat(str(path_to_file[0]), squeeze_me=True)['features'].ravel()
        # a = loadmat(str(path_to_file[1]), squeeze_me=True)['features'].ravel(

#        print(path_to_file)
        
        m = np.load(str(path_to_file[0]))
        a = np.load(str(path_to_file[1]))

        if self.megind is not None:
            m = m[:, self.megind].squeeze()

        # m = zscore(m)
        # a = zscore(a)

#        if np.isnan(m.max()):
#            print('Bad MEG file.')
#            exit()
#        elif np.isnan(a.max()):
#            print('Bad Audio File.')
#            exit()

        return np.concatenate((m.ravel(), a.ravel()))

        # return loadmat(path_to_file, squeeze_me=True)['features'].reshape(-1)

    def _load(self, batch: np.ndarray, cols: list):
        """
        Provided a batch of (subject, path_to_file), load the x and y vectors of data
        :param batch: The batch of filenames to load
        :return: (x_data, y_data)
        """
        x = np.zeros([batch.shape[0], self.longest_vector])
        y = np.zeros([batch.shape[0], 1])

        for row in range(x.shape[0]):
            ep = tuple(batch[row][1:])
            temp = self.get_flattened_features(ep)
            x[row, :len(temp)] = temp
            y[row, :] = np.array([self.subject_hash[batch[row][0]][column] for column in cols])

        return x[~np.isnan(x).any(axis=1)], y[~np.isnan(x).any(axis=1)]

    def files_to_load(self, tests):
        longest_vector = -1
        slice_length = -1
        loaded_subjects = {}
        for subject in [x for x in self.toplevel.iterdir() if x.is_dir() and x.name in self.subject_hash.keys()]:
            print('Loading subject', subject.stem, '...')
            loaded_subjects[subject.stem] = []

            # Determine overlap of MEG and Audio data
            audiotests = {t.stem: t for t in (subject / self.modality_folders[0]).iterdir() if t.name in tests}
            megtests = {t.stem: t for t in (subject / self.modality_folders[1]).iterdir() if t.name in tests}

            matched = set(audiotests.keys()).intersection(set(megtests.keys()))

            for experiment in matched:

                audioepochs = {x.stem: x for x in audiotests[experiment].iterdir() if x.suffix == LOAD_SUFFIX}
                megepochs = {x.stem: x for x in megtests[experiment].iterdir() if x.suffix == LOAD_SUFFIX}
                matched = set(audioepochs.keys()).intersection(set(megepochs.keys()))

                for epoch in matched:
                    try:
                        # megf = loadmat(str(megepochs[epoch]), squeeze_me=True)
                        # audf = loadmat(str(audioepochs[epoch]), squeeze_me=True)

                        megf = np.load(str(megepochs[epoch]))
                        audf = np.load(str(audioepochs[epoch]))
                        slice_length = max(slice_length, megf.shape[1] + audf.shape[1])
                        longest_vector = max(longest_vector, megf.shape[0]*megf.shape[1] + audf.shape[0]*audf.shape[1])

                        # slice_length = max(slice_length, len(megf['header']) + len(audf['header']))
                        # longest_vector = max(longest_vector,
                        #                      len(megf['features'].reshape(-1)) + len(audf['features'].reshape(-1)))
                        loaded_subjects[subject.stem].append((subject.stem, audioepochs[epoch], megepochs[epoch]))
                    except Exception:
                        print('Warning: Skipping file, error occurred loading:', epoch)

        return loaded_subjects, longest_vector, slice_length

    @property
    def modality_folders(self):
        return AcousticDataset.modality_folders.fget(self) + MEGDataset.modality_folders.fget(self)


class FusionAgeRangesDataset(FusionDataset, BaseDatasetAgeRanges):
    pass


# Main is for testing only
if __name__ == '__main__':
    # dataset = MEGDataset('/mnt/elephant_sized_space/FEATURES/')
    dataset = AcousticDataset('/mnt/elephant_sized_space/ACOUSTIC_DATASET/')

    while True:
        try:
            for xb, yb in dataset:
                print('batch')
        except TrainingComplete:
            print('Trained single model')
            continue
        except CrossValidationComplete:
            print('Finished whole model')
            break

