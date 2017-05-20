# Copyright (C) 2015 UCSC Computational Genomics Lab
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
import logging
import os
import stat
from pipes import quote
import subprocess
import time
import math
import sys
import xml.etree.ElementTree as ET
import tempfile

from toil import resolveEntryPoint
from toil.batchSystems import MemoryString
from toil.batchSystems.abstractGridEngineBatchSystem import AbstractGridEngineBatchSystem

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)



class TorqueBatchSystem(AbstractGridEngineBatchSystem):
    


    # class-specific Worker
    class Worker(AbstractGridEngineBatchSystem.Worker):

        """
        Torque-specific AbstractGridEngineWorker methods
        """
        def getRunningJobIDs(self):
            times = {}
            currentjobs = dict((str(self.batchJobIDs[x][0].strip()), x) for x in self.runningJobs)
            logger.debug("getRunningJobIDs current jobs are: " + str(currentjobs))
            # Limit qstat to current username to avoid clogging the batch system on heavily loaded clusters
            #job_user = os.environ.get('USER')
            #process = subprocess.Popen(['qstat', '-u', job_user], stdout=subprocess.PIPE)
            # -x shows exit status in PBSPro
            process = subprocess.Popen(['qstat', '-x'], stdout=subprocess.PIPE)
            stdout, stderr = process.communicate()

            # qstat supports XML output which is more comprehensive, but PBSPro does not support it 
            # so instead we stick with plain commandline "qstat" outputs
            for currline in stdout.split('\n'):
                items = currline.strip().split()
                if items:
                    jobid = items[0].strip()
                    if jobid in currentjobs:
                        logger.debug("getRunningJobIDs job status for is: " + items[4])
                    if jobid in currentjobs and items[4] == 'R':
                        walltime = items[3]
                        logger.debug("getRunningJobIDs qstat reported walltime is: " + walltime)
                        # normal qstat has a quirk with job time where it reports '0'
                        # when initially running; this catches this case
                        if walltime == '0':
                            walltime = time.mktime(time.strptime(walltime, "%S"))
                        else:
                            walltime = time.mktime(time.strptime(walltime, "%H:%M:%S"))
                        times[currentjobs[jobid]] = walltime

            logger.debug("Job times from qstat are: " + str(times))
            return times

        def getUpdatedBatchJob(self, maxWait):
            try:
                logger.debug("getUpdatedBatchJob AAAAAAAA")
                pbsJobID, retcode = self.updatedJobsQueue.get(timeout=maxWait)
                self.updatedJobsQueue.task_done()
                jobID, retcode = (self.jobIDs[pbsJobID], retcode)
                self.currentjobs -= {self.jobIDs[pbsJobID]}
            except Empty:
                logger.debug("getUpdatedBatchJob BBBBBBB")
                pass
            else:
                return jobID, retcode, None

        def killJob(self, jobID):
            subprocess.check_call(['qdel', self.getBatchSystemID(jobID)])

        def prepareSubmission(self, cpu, memory, jobID, command):
            return self.prepareQsub(cpu, memory, jobID) + [self.generateTorqueWrapper(command)]

        def submitJob(self, subLine):
            process = subprocess.Popen(subLine, stdout=subprocess.PIPE)
            so, se = process.communicate()
            result = so
            # TODO: the full URI here may be needed on complex setups, stripping
            # down to integer job ID only may be bad long-term
            #logger.debug("Submitting job with: {}\n".format(so))
            #if so is not '':
            #    result = int(so.strip().split('.')[0])
            return result

        def getJobExitCode(self, torqueJobID):
            args = ["qstat", "-x", "-f", str(torqueJobID).split('.')[0]]

            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for line in process.stdout:
                line = line.strip()
                #logger.debug("getJobExitCode exit status: " + line)
                # Case differences due to PBSPro vs OSS Torque qstat outputs
                if line.startswith("failed") or line.startswith("FAILED") and int(line.split()[1]) == 1:
                    return 1
                if line.startswith("exit_status") or line.startswith("Exit_status"):
                    status = line.split(' = ')[1]
                    logger.debug('Exit Status: ' + status)
                    return int(status)

        """
        Implementation-specific helper methods
        """
        def prepareQsub(self, cpu, mem, jobID):

            # TODO: passing $PWD on command line not working for -d, resorting to
            # $PBS_O_WORKDIR but maybe should fix this here instead of in script?

            # TODO: we previosuly trashed the stderr/stdout, as in the commented
            # code, but these may be retained by others, particularly for debugging.
            # Maybe an option or attribute w/ a location for storing the logs?

            # qsubline = ['qsub', '-V', '-j', 'oe', '-o', '/dev/null',
            #             '-e', '/dev/null', '-N', 'toil_job_{}'.format(jobID)]

            # Passing -V overwrites the environment
            #qsubline = ['qsub', '-V', '-N', 'toil_job_{}'.format(jobID)]
            qsubline = ['qsub', '-N', 'toil_job_{}'.format(jobID), '-j', 'oe', '-e', 'cwltoil_pbspro_err.log', '-o', 'cwltoil_pbspro_out.log']
            #qsubline.append('-V')
            qsubline.append('-v')
            qsubline.append('PATH')
            qsubline.append('-v')
            qsubline.append('PROJECT')

            if self.boss.environment:
                qsubline.append('-v')
                qsubline.append(','.join(k + '=' + quote(os.environ[k] if v is None else v)
                                         for k, v in self.boss.environment.iteritems()))

            reqline = list()
            if mem is not None:
                memStr = str(mem / 1024) + 'K'
                reqline += ['-l mem=' + memStr]

            if cpu is not None and math.ceil(cpu) > 1:
                qsubline.extend(['-l ncpus=' + str(int(math.ceil(cpu)))])

            return qsubline

        def generateTorqueWrapper(self, command):
            """
            A very simple script generator that just wraps the command given; for
            now this goes to default tempdir
            """
            _, tmpFile = tempfile.mkstemp(suffix='.sh', prefix='torque_wrapper')
            #venv_prefix = "source activate root"
            fh = open(tmpFile , 'w')
            fh.write("#!/bin/sh\n")
            fh.write("#PBS -q normalsp\n")
            #fh.write("#PBS -l walltime=00:10:00\n")
            fh.write("#PBS -e torque_run_wrapper_err.log\n")
            fh.write("#PBS -o torque_run_wrapper_out.log\n\n")
            fh.write("cd $PBS_O_WORKDIR\n\n")
            #fh.write(venv_prefix + " && ")
            fh.write(command + "\n")

            fh.close
            logger.debug('Chmod wrapper with exec permissions: ' + tmpFile)
            os.chmod(tmpFile, stat.S_IEXEC | stat.S_IXGRP | stat.S_IRUSR)
            
            return tmpFile

    @classmethod
    def obtainSystemConstants(cls):

        # See: https://github.com/BD2KGenomics/toil/pull/1617#issuecomment-293525747
        logger.debug("PBS/Torque does not need obtainSystemConstants to assess global cluster resources.")


        #return maxCPU, maxMEM
        return None, None
