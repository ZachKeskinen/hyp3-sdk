import math
import time
import warnings
from datetime import datetime
from functools import singledispatchmethod
from typing import List, Optional, Union, Literal, TypedDict
from urllib.parse import urljoin

from requests.exceptions import HTTPError, RequestException
from tqdm.auto import tqdm

import hyp3_sdk
from hyp3_sdk.exceptions import HyP3Error
from hyp3_sdk.jobs import Batch, Job
from hyp3_sdk.util import get_authenticated_session

HYP3_PROD = 'https://hyp3-api.asf.alaska.edu'
HYP3_TEST = 'https://hyp3-test-api.asf.alaska.edu'


class HyP3:
    """A python wrapper around the HyP3 API"""

    def __init__(self, api_url: str = HYP3_PROD, username: Optional = None, password: Optional = None):
        """
        Args:
            api_url: Address of the HyP3 API
            username: Username for authenticating to urs.earthdata.nasa.gov.
                Both username and password must be provided if either is provided.
            password: Password for authenticating to urs.earthdata.nasa.gov.
               Both username and password must be provided if either is provided.
        """
        self.url = api_url
        self.session = get_authenticated_session(username, password)
        self.session.headers.update({'User-Agent': f'{hyp3_sdk.__name__}/{hyp3_sdk.__version__}'})

    def find_jobs(self, start: Optional[datetime] = None, end: Optional[datetime] = None,
                  status: Optional[str] = None, name: Optional[str] = None) -> Batch:
        """Gets a Batch of jobs from HyP3 matching the provided search criteria

        Args:
            start: only jobs submitted after given time
            end: only jobs submitted before given time
            status: only jobs matching this status (SUCCEEDED, FAILED, RUNNING, PENDING)
            name: only jobs with this name

        Returns:
            A Batch object containing the found jobs
        """
        params = {}
        if name is not None:
            params['name'] = name
        if start is not None:
            params['start'] = start.isoformat(timespec='seconds')
            if start.tzinfo is None:
                params['start'] += 'Z'
        if end is not None:
            params['end'] = end.isoformat(timespec='seconds')
            if end.tzinfo is None:
                params['end'] += 'Z'
        if status is not None:
            params['status_code'] = status

        response = self.session.get(urljoin(self.url, '/jobs'), params=params)
        try:
            response.raise_for_status()
        except HTTPError:
            raise HyP3Error(f'Error while trying to query {response.url}')
        jobs = [Job.from_dict(job) for job in response.json()['jobs']]
        if not jobs:
            warnings.warn('Found zero jobs', UserWarning)
        return Batch(jobs)

    def get_job_by_id(self, job_id: str) -> Job:
        """Get job by job ID

        Args:
            job_id: A job ID

        Returns:
            A Job object
        """
        try:
            response = self.session.get(urljoin(self.url, f'/jobs/{job_id}'))
            response.raise_for_status()
        except RequestException:
            raise HyP3Error(f'Unable to get job by ID {job_id}')
        return Job.from_dict(response.json())

    @singledispatchmethod
    def watch(self, job_or_batch: Union[Batch, Job], timeout: int = 10800, interval: Union[int, float] = 60):
        """Watch jobs until they complete

        Args:
            job_or_batch: A Batch or Job object of jobs to watch
            timeout: How long to wait until exiting in seconds
            interval: How often to check for updates in seconds

        Returns:
            A Batch or Job object with refreshed watched jobs
        """
        raise NotImplementedError(f'Cannot watch {type(job_or_batch)} type object')

    @watch.register
    def _watch_batch(self, batch: Batch, timeout: int = 10800, interval: Union[int, float] = 60):
        iterations_until_timeout = math.ceil(timeout / interval)
        bar_format = '{l_bar}{bar}| {n_fmt}/{total_fmt} [{postfix[0]}]'
        with tqdm(total=len(batch), bar_format=bar_format, postfix=[f'timeout in {timeout} s']) as progress_bar:
            for ii in range(iterations_until_timeout):
                batch = self.refresh(batch)

                counts = batch._count_statuses()
                complete = counts['SUCCEEDED'] + counts['FAILED']

                progress_bar.postfix = [f'timeout in {timeout - ii * interval}s']
                # to control n/total manually; update is n += value
                progress_bar.n = complete
                progress_bar.update(0)

                if batch.complete():
                    return batch
                time.sleep(interval)
        raise HyP3Error(f'Timeout occurred while waiting for {batch}')

    @watch.register
    def _watch_job(self, job: Job, timeout: int = 10800, interval: Union[int, float] = 60):
        iterations_until_timeout = math.ceil(timeout / interval)
        bar_format = '{n_fmt}/{total_fmt} [{postfix[0]}]'
        with tqdm(total=1, bar_format=bar_format, postfix=[f'timeout in {timeout} s']) as progress_bar:
            for ii in range(iterations_until_timeout):
                job = self.refresh(job)
                progress_bar.postfix = [f'timeout in {timeout - ii * interval}s']
                progress_bar.update(int(job.complete()))

                if job.complete():
                    return job
                time.sleep(interval)
        raise HyP3Error(f'Timeout occurred while waiting for {job}')

    @singledispatchmethod
    def refresh(self, job_or_batch: Union[Batch, Job]) -> Union[Batch, Job]:
        """Refresh each jobs' information

        Args:
            job_or_batch: A Batch of Job object to refresh

        Returns:
            A Batch or Job object with refreshed information
        """
        raise NotImplementedError(f'Cannot refresh {type(job_or_batch)} type object')

    @refresh.register
    def _refresh_batch(self, batch: Batch):
        jobs = []
        for job in batch.jobs:
            jobs.append(self.refresh(job))
        return Batch(jobs)

    @refresh.register
    def _refresh_job(self, job: Job):
        return self.get_job_by_id(job.job_id)

    def submit_prepared_jobs(self, prepared_jobs: Union[dict, List[dict]]) -> Batch:
        """Submit a prepared job dictionary, or list of prepared job dictionaries

        Args:
            prepared_jobs: A prepared job dictionary, or list of prepared job dictionaries

        Returns:
            A Batch object containing the submitted job(s)
        """
        if isinstance(prepared_jobs, dict):
            payload = {'jobs': [prepared_jobs]}
        else:
            payload = {'jobs': prepared_jobs}

        response = self.session.post(urljoin(self.url, '/jobs'), json=payload)
        try:
            response.raise_for_status()
        except HTTPError as e:
            raise HyP3Error(str(e))

        batch = Batch()
        for job in response.json()['jobs']:
            batch += Job.from_dict(job)
        return batch

    def submit_autorift_job(self, granule1: str, granule2: str, name: Optional[str] = None) -> Batch:
        """Submit an autoRIFT job

        Args:
            granule1: The first granule (scene) to use
            granule2: The second granule (scene) to use
            name: A name for the job

        Returns:
            A Batch object containing the autoRIFT job
        """
        job_dict = self.prepare_autorift_job(granule1, granule2, name=name)
        return self.submit_prepared_jobs(prepared_jobs=job_dict)

    @classmethod
    def prepare_autorift_job(cls, granule1: str, granule2: str, name: Optional[str] = None) -> dict:
        """Submit an autoRIFT job

        Args:
            granule1: The first granule (scene) to use
            granule2: The second granule (scene) to use
            name: A name for the job

        Returns:
            A dictionary containing the prepared autoRIFT job
        """
        job_dict = {
            'job_parameters': {'granules': [granule1, granule2]},
            'job_type': 'AUTORIFT',
        }
        if name is not None:
            job_dict['name'] = name
        return job_dict

    def submit_rtc_job(self,
                       granule: str,
                       name: Optional[str] = None,
                       dem_matching: Optional[bool] = None,
                       include_dem: Optional[bool] = None,
                       include_inc_map: Optional[bool] = None,
                       include_scattering_area: Optional[bool] = None,
                       radiometry: Union[Literal['gamma0'], Literal['sigma0']] = None,
                       resolution: Literal[30] = None,
                       scale: Union[Literal['power'], Literal['amplitude']] = None,
                       speckle_filter: Optional[bool] = None,
                       **kwargs) -> Batch:
        """Submit an RTC job

        Args:
            granule: The granule (scene) to use
            name: A name for the job
            dem_matching: Coregisters SAR data to the DEM, rather than using dead reckoning based on orbit files
            include_dem: Include the DEM file in the product package
            include_inc_map: Include the incidence angle map in the product package
            include_scattering_area: Include the scattering area in the product package
            radiometry: Backscatter coefficient normalization, either by ground area (sigma0) or illuminated area projected into the look direction (gamma0)
            resolution: Desired output pixel spacing in meters
            scale: Scale of output image; either power or amplitude
            speckle_filter: Apply an Enhanced Lee speckle filter
            **kwargs: Extra job parameters specifying custom processing options

        Returns:
            A Batch object containing the RTC job
        """
        job_dict = self.prepare_rtc_job(granule,
                                        name=name,
                                        dem_matching=dem_matching,
                                        include_dem=include_dem,
                                        include_inc_map=include_inc_map,
                                        include_scattering_area=include_scattering_area,
                                        radiometry=radiometry,
                                        resolution=resolution,
                                        scale=scale,
                                        speckle_filter=speckle_filter,
                                        **kwargs)
        return self.submit_prepared_jobs(prepared_jobs=job_dict)

    @classmethod
    def prepare_rtc_job(cls,
                        granule: str,
                        name: Optional[str] = None,
                        dem_matching: Optional[bool] = None,
                        include_dem: Optional[bool] = None,
                        include_inc_map: Optional[bool] = None,
                        include_scattering_area: Optional[bool] = None,
                        radiometry: Union[Literal['gamma0'], Literal['sigma0']] = None,
                        resolution: Literal[30] = None,
                        scale: Union[Literal['power'], Literal['amplitude']] = None,
                        speckle_filter: Optional[bool] = None,
                        **kwargs) -> dict:
        """Submit an RTC job

        Args:
            granule: The granule (scene) to use
            name: A name for the job
            dem_matching: Coregisters SAR data to the DEM, rather than using dead reckoning based on orbit files
            include_dem: Include the DEM file in the product package
            include_inc_map: Include the incidence angle map in the product package
            include_scattering_area: Include the scattering area in the product package
            radiometry: Backscatter coefficient normalization, either by ground area (sigma0) or illuminated area projected into the look direction (gamma0)
            resolution: Desired output pixel spacing in meters
            scale: Scale of output image; either power or amplitude
            speckle_filter: Apply an Enhanced Lee speckle filter
            **kwargs: Extra job parameters specifying custom processing options

        Returns:
            A dictionary containing the prepared RTC job
        """
        job_parameters = {
            'dem_matching': dem_matching,
            'include_dem': include_dem,
            'include-inc_map': include_inc_map,
            'include_scatteering_area': include_scattering_area,
            'radiometry': radiometry,
            'resolution': resolution,
            'scale': scale,
            'speckle_filter': speckle_filter,
            **kwargs
        }
        for k, v in job_parameters:
            if v is None:
                del job_parameters[k]
        job_dict = {
            'job_parameters': {'granules': [granule], **job_parameters},
            'job_type': 'RTC_GAMMA',
        }
        if name is not None:
            job_dict['name'] = name
        return job_dict

    def submit_insar_job(self,
                         granule1: str,
                         granule2: str,
                         name: Optional[str] = None,
                         include_look_vectors: Optional[bool] = None,
                         include_los_displacement: Optional[bool] = None,
                         looks: Union[Literal['20x4'], Literal['10x2']] = None,
                         **kwargs) -> Batch:
        """Submit an InSAR job

        Args:
            granule1: The first granule (scene) to use
            granule2: The second granule (scene) to use
            name: A name for the job
            include_look_vectors: Include the look vector theta and phi files in the product package
            include_los_displacement: Include a GeoTIFF in the product package containing displacement values along the Line-Of-Sight (LOS)
            looks: Number of looks to take in range and azimuth
            **kwargs: Extra job parameters specifying custom processing options

        Returns:
            A Batch object containing the InSAR job
        """
        job_dict = self.prepare_insar_job(granule1, granule2, name=name, **kwargs)
        return self.submit_prepared_jobs(prepared_jobs=job_dict)

    @classmethod
    def prepare_insar_job(cls,
                          granule1: str,
                          granule2: str,
                          name: Optional[str] = None,
                          include_look_vectors: Optional[bool] = None,
                          include_los_displacement: Optional[bool] = None,
                          looks: Union[Literal['20x4'], Literal['10x2']] = None,
                          **kwargs) -> dict:
        """Submit an InSAR job

        Args:
            granule1: The first granule (scene) to use
            granule2: The second granule (scene) to use
            name: A name for the job
            include_look_vectors: Include the look vector theta and phi files in the product package
            include_los_displacement: Include a GeoTIFF in the product package containing displacement values along the Line-Of-Sight (LOS)
            looks: Number of looks to take in range and azimuth
            **kwargs: Extra job parameters specifying custom processing options

        Returns:
            A dictionary containing the prepared InSAR job
        """
        job_parameters = {
            'include_look_vectors': include_look_vectors,
            'include_los_displacement': include_los_displacement,
            'looks': looks,
            **kwargs
        }
        for k, v in job_parameters:
            if v is None:
                del job_parameters[k]
        job_dict = {
            'job_parameters': {'granules': [granule1, granule2], **job_parameters},
            'job_type': 'INSAR_GAMMA',
        }
        if name is not None:
            job_dict['name'] = name
        return job_dict

    def my_info(self) -> dict:
        """
        Returns:
            Your user information
        """
        try:
            response = self.session.get(urljoin(self.url, '/user'))
            response.raise_for_status()
        except HTTPError:
            raise HyP3Error('Unable to get user information from API')
        return response.json()

    def check_quota(self) -> int:
        """
        Returns:
            The number of jobs left in your quota
        """
        info = self.my_info()
        return info['quota']['remaining']
