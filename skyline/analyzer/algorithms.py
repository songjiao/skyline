from __future__ import division
import logging
from time import time
from os import getpid
from timeit import default_timer as timer

# @added 20200117 - Feature #3400: Identify air gaps in the metric data
from collections import Counter
from ast import literal_eval

import pandas
import numpy as np
import scipy
import statsmodels.api as sm
import traceback

from settings import (
    ALGORITHMS,
    CONSENSUS,
    FULL_DURATION,
    MAX_TOLERABLE_BOREDOM,
    MIN_TOLERABLE_LENGTH,
    STALE_PERIOD,
    REDIS_SOCKET_PATH,
    ENABLE_SECOND_ORDER,
    BOREDOM_SET_SIZE,
    PANDAS_VERSION,
    RUN_OPTIMIZED_WORKFLOW,
    SKYLINE_TMP_DIR,
    ENABLE_ALGORITHM_RUN_METRICS,
    ENABLE_ALL_ALGORITHMS_RUN_METRICS,
    REDIS_PASSWORD,
    # @added 20200117 - Feature #3400: Identify air gaps in the metric data
    FULL_NAMESPACE,
)

from algorithm_exceptions import TooShort, Stale, Boring

if ENABLE_SECOND_ORDER:
    from redis import StrictRedis
    from msgpack import unpackb, packb
    # @modified 20180519 - Feature #2378: Add redis auth to Skyline and rebrow
    if REDIS_PASSWORD:
        redis_conn = StrictRedis(password=REDIS_PASSWORD, unix_socket_path=REDIS_SOCKET_PATH)
    else:
        redis_conn = StrictRedis(unix_socket_path=REDIS_SOCKET_PATH)

skyline_app = 'analyzer'
skyline_app_logger = '%sLog' % skyline_app
logger = logging.getLogger(skyline_app_logger)

try:
    send_algorithm_run_metrics = ENABLE_ALGORITHM_RUN_METRICS
except:
    send_algorithm_run_metrics = False

# @added 20180807 - Feature #2492: alert on stale metrics
try:
    from settings import ALERT_ON_STALE_METRICS
#    ALERT_ON_STALE_METRICS = settings.ALERT_ON_STALE_METRICS
except:
    ALERT_ON_STALE_METRICS = False
try:
    from settings import ALERT_ON_STALE_PERIOD
#    ALERT_ON_STALE_PERIOD = settings.ALERT_ON_STALE_PERIOD
except:
    ALERT_ON_STALE_PERIOD = 300
# @added 20200117 - Feature #3400: Identify air gaps in the metric data
try:
    from settings import IDENTIFY_AIRGAPS
except:
    IDENTIFY_AIRGAPS = False
try:
    from settings import MAX_AIRGAP_PERIOD
except:
    MAX_AIRGAP_PERIOD = int(3600 * 6)
# @added 20200214 - Bug #3448: Repeated airgapped_metrics
#                   Feature #3400: Identify air gaps in the metric data
try:
    from settings import IDENTIFY_UNORDERED_TIMESERIES
except:
    IDENTIFY_UNORDERED_TIMESERIES = False
try:
    from settings import CHECK_AIRGAPS
except:
    CHECK_AIRGAPS = []
try:
    from settings import SKIP_AIRGAPS
except:
    SKIP_AIRGAPS = []

"""
This is no man's land. Do anything you want in here,
as long as you return a boolean that determines whether the input timeseries is
anomalous or not.

The key here is to return a True or False boolean.

You should use the pythonic except mechanism to ensure any excpetions do not
cause things to halt and the record_algorithm_error utility can be used to
sample any algorithm errors to log.

To add an algorithm, define it here, and add its name to settings.ALGORITHMS.
"""


def tail_avg(timeseries):
    """
    This is a utility function used to calculate the average of the last three
    datapoints in the series as a measure, instead of just the last datapoint.
    It reduces noise, but it also reduces sensitivity and increases the delay
    to detection.
    """
    try:
        t = (timeseries[-1][1] + timeseries[-2][1] + timeseries[-3][1]) / 3
        return t
    except IndexError:
        return timeseries[-1][1]


def median_absolute_deviation(timeseries):
    """
    A timeseries is anomalous if the deviation of its latest datapoint with
    respect to the median is X times larger than the median of deviations.
    """
    # logger.info('Running ' + str(get_function_name()))
    try:
        series = pandas.Series([x[1] for x in timeseries])
        median = series.median()
        demedianed = np.abs(series - median)
        median_deviation = demedianed.median()
    except:
        traceback_format_exc_string = traceback.format_exc()
        algorithm_name = str(get_function_name())
        record_algorithm_error(algorithm_name, traceback_format_exc_string)
        return None

    # The test statistic is infinite when the median is zero,
    # so it becomes super sensitive. We play it safe and skip when this happens.
    if median_deviation == 0:
        return False

    if PANDAS_VERSION < '0.17.0':
        try:
            test_statistic = demedianed.iget(-1) / median_deviation
        except:
            traceback_format_exc_string = traceback.format_exc()
            algorithm_name = str(get_function_name())
            record_algorithm_error(algorithm_name, traceback_format_exc_string)
            return None
    else:
        try:
            test_statistic = demedianed.iat[-1] / median_deviation
        except:
            traceback_format_exc_string = traceback.format_exc()
            algorithm_name = str(get_function_name())
            record_algorithm_error(algorithm_name, traceback_format_exc_string)
            return None

    # Completely arbitary...triggers if the median deviation is
    # 6 times bigger than the median
    if test_statistic > 6:
        return True

    # As per https://github.com/etsy/skyline/pull/104 by @rugger74
    # Although never seen this should return False if not > arbitary_value
    # 20160523 @earthgecko
    return False


def grubbs(timeseries):
    """
    A timeseries is anomalous if the Z score is greater than the Grubb's score.
    """

    try:
        # @modified 20191011 - Update least_squares & grubbs algorithms by using sample standard deviation PR #124
        #                      Task #3256: Review and test PR 124
        # Change from using scipy/numpy std which calculates the population
        # standard deviation to using pandas.std which calculates the sample
        # standard deviation which is more appropriate for time series data
        # series = scipy.array([x[1] for x in timeseries])
        # stdDev = scipy.std(series)
        series = pandas.Series(x[1] for x in timeseries)
        stdDev = series.std()

        # Issue #27 - Handle z_score agent.py RuntimeWarning - https://github.com/earthgecko/skyline/issues/27
        # This change avoids spewing warnings on agent.py tests:
        # RuntimeWarning: invalid value encountered in double_scalars
        # If stdDev is 0 division returns nan which is not > grubbs_score so
        # return False here
        if stdDev == 0:
            return False

        mean = np.mean(series)
        tail_average = tail_avg(timeseries)
        z_score = (tail_average - mean) / stdDev
        len_series = len(series)
        threshold = scipy.stats.t.isf(.05 / (2 * len_series), len_series - 2)
        threshold_squared = threshold * threshold
        grubbs_score = ((len_series - 1) / np.sqrt(len_series)) * np.sqrt(threshold_squared / (len_series - 2 + threshold_squared))

        return z_score > grubbs_score
    except:
        traceback_format_exc_string = traceback.format_exc()
        algorithm_name = str(get_function_name())
        record_algorithm_error(algorithm_name, traceback_format_exc_string)
        return None


def first_hour_average(timeseries):
    """
    Calcuate the simple average over one hour, FULL_DURATION seconds ago.
    A timeseries is anomalous if the average of the last three datapoints
    are outside of three standard deviations of this value.
    """

    try:
        last_hour_threshold = time() - (FULL_DURATION - 3600)
        series = pandas.Series([x[1] for x in timeseries if x[0] < last_hour_threshold])
        mean = (series).mean()
        stdDev = (series).std()
        t = tail_avg(timeseries)

        return abs(t - mean) > 3 * stdDev
    except:
        traceback_format_exc_string = traceback.format_exc()
        algorithm_name = str(get_function_name())
        record_algorithm_error(algorithm_name, traceback_format_exc_string)
        return None


def stddev_from_average(timeseries):
    """
    A timeseries is anomalous if the absolute value of the average of the latest
    three datapoint minus the moving average is greater than three standard
    deviations of the average. This does not exponentially weight the MA and so
    is better for detecting anomalies with respect to the entire series.
    """

    try:
        series = pandas.Series([x[1] for x in timeseries])
        mean = series.mean()
        stdDev = series.std()
        t = tail_avg(timeseries)

        return abs(t - mean) > 3 * stdDev
    except:
        traceback_format_exc_string = traceback.format_exc()
        algorithm_name = str(get_function_name())
        record_algorithm_error(algorithm_name, traceback_format_exc_string)
        return None


def stddev_from_moving_average(timeseries):
    """
    A timeseries is anomalous if the absolute value of the average of the latest
    three datapoint minus the moving average is greater than three standard
    deviations of the moving average. This is better for finding anomalies with
    respect to the short term trends.
    """
    try:
        series = pandas.Series([x[1] for x in timeseries])
        if PANDAS_VERSION < '0.18.0':
            expAverage = pandas.stats.moments.ewma(series, com=50)
            stdDev = pandas.stats.moments.ewmstd(series, com=50)
        else:
            expAverage = pandas.Series.ewm(series, ignore_na=False, min_periods=0, adjust=True, com=50).mean()
            stdDev = pandas.Series.ewm(series, ignore_na=False, min_periods=0, adjust=True, com=50).std(bias=False)

        if PANDAS_VERSION < '0.17.0':
            return abs(series.iget(-1) - expAverage.iget(-1)) > 3 * stdDev.iget(-1)
        else:
            return abs(series.iat[-1] - expAverage.iat[-1]) > 3 * stdDev.iat[-1]
# http://stackoverflow.com/questions/28757389/loc-vs-iloc-vs-ix-vs-at-vs-iat
    except:
        traceback_format_exc_string = traceback.format_exc()
        algorithm_name = str(get_function_name())
        record_algorithm_error(algorithm_name, traceback_format_exc_string)
        return None


def mean_subtraction_cumulation(timeseries):
    """
    A timeseries is anomalous if the value of the next datapoint in the
    series is farther than three standard deviations out in cumulative terms
    after subtracting the mean from each data point.
    """

    try:
        series = pandas.Series([x[1] if x[1] else 0 for x in timeseries])
        series = series - series[0:len(series) - 1].mean()
        stdDev = series[0:len(series) - 1].std()
        # @modified 20161228 - Feature #1828: ionosphere - mirage Redis data features
        # This expAverage is unused
        # if PANDAS_VERSION < '0.18.0':
        #     expAverage = pandas.stats.moments.ewma(series, com=15)
        # else:
        #     expAverage = pandas.Series.ewm(series, ignore_na=False, min_periods=0, adjust=True, com=15).mean()

        if PANDAS_VERSION < '0.17.0':
            return abs(series.iget(-1)) > 3 * stdDev
        else:
            return abs(series.iat[-1]) > 3 * stdDev
    except:
        traceback_format_exc_string = traceback.format_exc()
        algorithm_name = str(get_function_name())
        record_algorithm_error(algorithm_name, traceback_format_exc_string)
        return None


def least_squares(timeseries):
    """
    A timeseries is anomalous if the average of the last three datapoints
    on a projected least squares model is greater than three sigma.
    """

    try:
        x = np.array([t[0] for t in timeseries])
        y = np.array([t[1] for t in timeseries])
        A = np.vstack([x, np.ones(len(x))]).T
        # @modified 20161228 - Feature #1828: ionosphere - mirage Redis data features
        # This results and residual are unused
        # results = np.linalg.lstsq(A, y)
        # residual = results[1]

        # @modified 20180910 - Task #2588: Update dependencies
        # Changed in version numpy 1.14.0: If not set, a FutureWarning is given.
        # The previous default of -1 will use the machine precision as rcond
        # parameter, the new default will use the machine precision times
        # max(M, N). To silence the warning and use the new default, use
        # rcond=None, to keep using the old behavior, use rcond=-1.
        # Tested with time series - /opt/skyline/ionosphere/features_profiles/stats/statsd/processing_time/1491468474/stats.statsd.processing_time.mirage.redis.24h.json
        # new rcond=None resulted in:
        # np.linalg.lstsq(A, y, rcond=None)[0]
        # >>> array([3.85656116e-11, 2.58582310e-20])
        # Original default results in:
        # np.linalg.lstsq(A, y, rcond=-1)[0]
        # >>> array([ 4.10251589e-07, -6.11801949e+02])
        # Changed to pass rcond=-1
        # m, c = np.linalg.lstsq(A, y)[0]
        m, c = np.linalg.lstsq(A, y, rcond=-1)[0]

        errors = []
        # Evaluate append once, not every time in the loop - this gains ~0.020 s on
        # every timeseries potentially @earthgecko #1310
        append_error = errors.append

        # Further a question exists related to performance and accruracy with
        # regards to how many datapoints are in the sample, currently all datapoints
        # are used but this may not be the ideal or most efficient computation or
        # fit for a timeseries... @earthgecko is checking graphite...
        for i, value in enumerate(y):
            projected = m * x[i] + c
            error = value - projected
            # errors.append(error) # @earthgecko #1310
            append_error(error)

        if len(errors) < 3:
            return False

        # @modified 20191011 - Update least_squares & grubbs algorithms by using sample standard deviation PR #124
        #                      Task #3256: Review and test PR 124
        # Change from using scipy/numpy std which calculates the population
        # standard deviation to using pandas.std which calculates the sample
        # standard deviation which is more appropriate for time series data
        # std_dev = scipy.std(errors)
        series = pandas.Series(x for x in errors)
        std_dev = series.std()

        t = (errors[-1] + errors[-2] + errors[-3]) / 3

        return abs(t) > std_dev * 3 and round(std_dev) != 0 and round(t) != 0
    except:
        traceback_format_exc_string = traceback.format_exc()
        algorithm_name = str(get_function_name())
        record_algorithm_error(algorithm_name, traceback_format_exc_string)
        return None


def histogram_bins(timeseries):
    """
    A timeseries is anomalous if the average of the last three datapoints falls
    into a histogram bin with less than 20 other datapoints (you'll need to tweak
    that number depending on your data)

    Returns: the size of the bin which contains the tail_avg. Smaller bin size
    means more anomalous.
    """

    try:
        series = scipy.array([x[1] for x in timeseries])
        t = tail_avg(timeseries)
        h = np.histogram(series, bins=15)
        bins = h[1]
        for index, bin_size in enumerate(h[0]):
            if bin_size <= 20:
                # Is it in the first bin?
                if index == 0:
                    if t <= bins[0]:
                        return True
                # Is it in the current bin?
                elif t >= bins[index] and t < bins[index + 1]:
                        return True

        return False
    except:
        traceback_format_exc_string = traceback.format_exc()
        algorithm_name = str(get_function_name())
        record_algorithm_error(algorithm_name, traceback_format_exc_string)
        return None


def ks_test(timeseries):
    """
    A timeseries is anomalous if 2 sample Kolmogorov-Smirnov test indicates
    that data distribution for last 10 minutes is different from last hour.
    It produces false positives on non-stationary series so Augmented
    Dickey-Fuller test applied to check for stationarity.
    """

    try:
        hour_ago = time() - 3600
        ten_minutes_ago = time() - 600
        reference = scipy.array([x[1] for x in timeseries if x[0] >= hour_ago and x[0] < ten_minutes_ago])
        probe = scipy.array([x[1] for x in timeseries if x[0] >= ten_minutes_ago])

        if reference.size < 20 or probe.size < 20:
            return False

        ks_d, ks_p_value = scipy.stats.ks_2samp(reference, probe)

        if ks_p_value < 0.05 and ks_d > 0.5:
            adf = sm.tsa.stattools.adfuller(reference, 10)
            if adf[1] < 0.05:
                return True

        return False
    except:
        traceback_format_exc_string = traceback.format_exc()
        algorithm_name = str(get_function_name())
        record_algorithm_error(algorithm_name, traceback_format_exc_string)
        return None

    return False


"""
THE END of NO MAN'S LAND


THE START of UTILITY FUNCTIONS

"""


def get_function_name():
    """
    This is a utility function is used to determine what algorithm is reporting
    an algorithm error when the record_algorithm_error is used.
    """
    return traceback.extract_stack(None, 2)[0][2]


def record_algorithm_error(algorithm_name, traceback_format_exc_string):
    """
    This utility function is used to facilitate the traceback from any algorithm
    errors.  The algorithm functions themselves we want to run super fast and
    without fail in terms of stopping the function returning and not reporting
    anything to the log, so the pythonic except is used to "sample" any
    algorithm errors to a tmp file and report once per run rather than spewing
    tons of errors into the log.

    .. note::
        algorithm errors tmp file clean up
            the algorithm error tmp files are handled and cleaned up in
            :class:`Analyzer` after all the spawned processes are completed.

    :param algorithm_name: the algoritm function name
    :type algorithm_name: str
    :param traceback_format_exc_string: the traceback_format_exc string
    :type traceback_format_exc_string: str
    :return:
        - ``True`` the error string was written to the algorithm_error_file
        - ``False`` the error string was not written to the algorithm_error_file

    :rtype:
        - boolean

    """

    current_process_pid = getpid()
    algorithm_error_file = '%s/%s.%s.%s.algorithm.error' % (
        SKYLINE_TMP_DIR, skyline_app, str(current_process_pid), algorithm_name)
    try:
        with open(algorithm_error_file, 'w') as f:
            f.write(str(traceback_format_exc_string))
        return True
    except:
        return False


def determine_median(timeseries):
    """
    Determine the median of the values in the timeseries
    """

    # logger.info('Running ' + str(get_function_name()))
    try:
        np_array = pandas.Series([x[1] for x in timeseries])
    except:
        return False
    try:
        array_median = np.median(np_array)
        return array_median
    except:
        return False

    return False


def determine_array_median(array):
    """
    Determine the median of the values in an array
    """
    try:
        np_array = np.array(array)
    except:
        return False

    # logger.info('Running ' + str(get_function_name()))
    try:
        array_median = np.median(np_array)
        return array_median
    except:
        return False

    return False


# @added 20200117 - Feature #3400: Identify air gaps in the metric data
# The implementation of this feature bumped up analyzer.run_time from:
# from ~2.5 to 3 seconds up to between 3.0 and 4.0 seconds on 608 metrics
# from ~5.5 to 10 seconds up to between 7.5 and 11.5 seconds on 1441 metrics
# from ~1.20 to 1.38 seconds up to between 1.42 and 1.5 seocnds on 191 metrics
def identify_airgaps(metric_name, timeseries, airgapped_metrics):
    """
    Identify air gaps in metrics to populate the analyzer.airgapped_metrics
    Redis set with the air gaps if the specific air gap it is not present in the
    set. If there is a start_airgap timestamp and no end_airgap is set then the
    metric will be in a current air gap state and/or it will become stale.  If
    the netric starts sending data again, it will have the end_airgap set and be
    added to the analyzer.airgapped_metrics Redis set.  Also Identify if a time
    series is unordered.

    :param metric_name: the FULL_NAMESPACE metric name
    :type metric_name: str
    :param timeseries: the metric time series
    :type timeseries: list
    :param airgapped_metrics: the air gapped metrics list generated from the
        analyzer.airgapped_metrics Redis set
    :type airgapped_metrics: list
    :return: list of air gapped metrics and a boolean as to whether the time
        series is unordered
    :rtype: list, boolean

    """

    if len(timeseries) < 30:
        return None

    airgaps = None
    # To ensure that nothing in this function affects existing analysis, them
    # entire block is wrapped in try except pass so that analyzer is affected
    # as little as possible should something here fail.
    try:
        current_timestamp = int(time())
        max_airgap_timestamp = current_timestamp - MAX_AIRGAP_PERIOD
        # Determine resolution from the data within the MAX_AIRGAP_PERIOD
        resolution_timestamps = []
        metric_resolution_determined = False
        for metric_datapoint in timeseries:
            timestamp = int(metric_datapoint[0])
            if timestamp < max_airgap_timestamp:
                continue
            resolution_timestamps.append(timestamp)
        timestamp_resolutions = []
        if resolution_timestamps:
            last_timestamp = None
            for timestamp in resolution_timestamps:
                if last_timestamp:
                    resolution = timestamp - last_timestamp
                    timestamp_resolutions.append(resolution)
                    last_timestamp = timestamp
                else:
                    last_timestamp = timestamp
        if resolution_timestamps:
            del resolution_timestamps
        timestamp_resolutions_count = None
        ordered_timestamp_resolutions_count = None
        metric_resolution = None
        if timestamp_resolutions:
            try:
                timestamp_resolutions_count = Counter(timestamp_resolutions)
                ordered_timestamp_resolutions_count = timestamp_resolutions_count.most_common()
                metric_resolution = int(ordered_timestamp_resolutions_count[0][0])
                if metric_resolution > 0:
                    metric_resolution_determined = True
            except:
                traceback_format_exc_string = traceback.format_exc()
                algorithm_name = str(get_function_name())
                record_algorithm_error(algorithm_name, traceback_format_exc_string)
                del timestamp_resolutions
                # return None
                return None, False
        if timestamp_resolutions:
            del timestamp_resolutions
        airgaps_present = False
        if metric_resolution_determined and metric_resolution:
            if metric_resolution < 600:
                airgap_duration = ((int(metric_resolution) * 2) + int(int(metric_resolution) / 2))
            else:
                airgap_duration = ((int(metric_resolution) * 2) + 60)
            for i in ordered_timestamp_resolutions_count:
                resolution = i[0]
                if resolution == metric_resolution:
                    continue
                if resolution > airgap_duration:
                    airgaps_present = True
        if timestamp_resolutions_count:
            del timestamp_resolutions_count

        # @added 20200214 - Bug #3448: Repeated airgapped_metrics
        #                   Feature #3400: Identify air gaps in the metric data
        # Identify metrics that have time series that are not ordered, for
        # Analyser to order and replace the existing Redis metric key time
        # data. If backfilling is being done via Flux then unordered time series
        # data can be expected from time to time.  Although these metrics are
        # identified via their flux.filled Redis key, this is an additional test
        # just to catch any that slip through the gaps.  This operation fairly
        # fast, testing on 657 metrics with loading all the time series data and
        # running the below function took 0.43558645248413086 seconds.
        unordered_timeseries = False
        for resolution in ordered_timestamp_resolutions_count:
            if resolution[0] < 0:
                unordered_timeseries = True
                break

        if ordered_timestamp_resolutions_count:
            del ordered_timestamp_resolutions_count

        # @added 20200214 - Bug #3448: Repeated airgapped_metrics
        #                      Feature #3400: Identify air gaps in the metric data
        # Here if airgaps are not being identifying, return whether the time
        # series is unordered
        if not IDENTIFY_AIRGAPS:
            del airgaps_present
            return None, unordered_timeseries
        if airgaps_present:
            base_name = metric_name.replace(FULL_NAMESPACE, '', 1)
            # logger.info('airgaps present in %s - %s' % (base_name, str(ordered_timestamp_resolutions_count)))
            airgaps = []
            last_timestamp = None
            start_airgap = None
            for metric_datapoint in timeseries:
                timestamp = int(metric_datapoint[0])
                # Handle the first timestamp
                if not last_timestamp:
                    last_timestamp = timestamp
                    continue
                # Discard any period less than MAX_AIRGAP_PERIOD
                if timestamp < max_airgap_timestamp:
                    last_timestamp = timestamp
                    continue
                original_last_timestamp = last_timestamp
                difference = timestamp - last_timestamp
                last_timestamp = timestamp
                if difference < airgap_duration:
                    if start_airgap:
                        end_airgap = original_last_timestamp - 1
                        airgap_known = False
                        if airgapped_metrics:
                            for i in airgapped_metrics:
                                # @modified 20200213 - Bug #3448: Repeated airgapped_metrics
                                # Only literal_eval if required
                                # airgap = literal_eval(i)
                                # airgap_metric = str(airgap[0])
                                # if base_name != airgap_metric:
                                if base_name in i:
                                    airgap = literal_eval(i)
                                else:
                                    continue
                                airgap_metric_resolution = int(airgap[1])
                                if metric_resolution != airgap_metric_resolution:
                                    continue
                                start_timestamp_present = False
                                airgap_metric_start_timestamp = int(airgap[2])
                                if start_airgap == airgap_metric_start_timestamp:
                                    start_timestamp_present = True
                                end_timestamp_present = False
                                airgap_metric_end_timestamp = int(airgap[3])
                                if end_airgap == airgap_metric_end_timestamp:
                                    end_timestamp_present = True
                                if start_timestamp_present and end_timestamp_present:
                                    airgap_known = True
                                    start_airgap = None
                                    end_airgap = None
                                    break
                        if not airgap_known:
                            # @modified 20200213 - Bug #3448: Repeated airgapped_metrics
                            add_airgap = True
                            if start_airgap < max_airgap_timestamp:
                                add_airgap = False
                            if end_airgap < max_airgap_timestamp:
                                add_airgap = False
                            if add_airgap:
                                airgaps.append([base_name, metric_resolution, start_airgap, end_airgap, 0])
                            start_airgap = None
                            end_airgap = None
                        continue
                if difference > airgap_duration:
                    if not start_airgap:
                        # If there is a start_airgap timestamp and no end_airgap
                        # is set then the metric will be in a current air gap
                        # state and/or it will become stale.  If the netric
                        # starts sending data again, it will have the end_airgap
                        # set and be added to airgapped_metrics
                        start_airgap = original_last_timestamp + 1
    except:
        traceback_format_exc_string = traceback.format_exc()
        algorithm_name = str(get_function_name())
        record_algorithm_error(algorithm_name, traceback_format_exc_string)
        # return None
        return None, False

    # @modified 20200214 - Bug #3448: Repeated airgapped_metrics
    #                      Feature #3400: Identify air gaps in the metric data
    # Also return with the time series is unordered
    # return airgaps
    return airgaps, unordered_timeseries


def is_anomalously_anomalous(metric_name, ensemble, datapoint):
    """
    This method runs a meta-analysis on the metric to determine whether the
    metric has a past history of triggering. TODO: weight intervals based on datapoint
    """
    # We want the datapoint to avoid triggering twice on the same data
    new_trigger = [time(), datapoint]

    # Get the old history
    raw_trigger_history = redis_conn.get('trigger_history.' + metric_name)
    if not raw_trigger_history:
        redis_conn.set('trigger_history.' + metric_name, packb([(time(), datapoint)]))
        return True

    trigger_history = unpackb(raw_trigger_history)

    # Are we (probably) triggering on the same data?
    if (new_trigger[1] == trigger_history[-1][1] and
            new_trigger[0] - trigger_history[-1][0] <= 300):
                return False

    # Update the history
    trigger_history.append(new_trigger)
    redis_conn.set('trigger_history.' + metric_name, packb(trigger_history))

    # Should we surface the anomaly?
    trigger_times = [x[0] for x in trigger_history]
    intervals = [
        trigger_times[i + 1] - trigger_times[i]
        for i, v in enumerate(trigger_times)
        if (i + 1) < len(trigger_times)
    ]

    series = pandas.Series(intervals)
    mean = series.mean()
    stdDev = series.std()

    return abs(intervals[-1] - mean) > 3 * stdDev


# @modified 20200117 - Feature #3400: Identify air gaps in the metric data
# Added the airgapped_metrics list
# def run_selected_algorithm(timeseries, metric_name):
def run_selected_algorithm(timeseries, metric_name, airgapped_metrics):
    """
    Filter timeseries and run selected algorithm.
    """

    # @added 20180807 - Feature #2492: alert on stale metrics
    # Determine if a metric has stopped sending data and if so add to the
    # analyzer.alert_on_stale_metrics Redis set
    if ALERT_ON_STALE_METRICS:
        add_to_alert_on_stale_metrics = False
        # @modified 20180816 - Feature #2492: alert on stale metrics
        # Added try and except to prevent some errors that are encounter between
        # 00:14 and 00:17 on some days
        # Traceback (most recent call last):
        # File "/opt/skyline/github/skyline/skyline/analyzer/analyzer.py", line 394, in spin_process
        # anomalous, ensemble, datapoint = run_selected_algorithm(timeseries, metric_name)
        # File "/opt/skyline/github/skyline/skyline/analyzer/algorithms.py", line 530, in run_selected_algorithm
        # if int(time()) - int(timeseries[-1][0]) >= ALERT_ON_STALE_PERIOD:
        # IndexError: list index out of range
        try:
            if int(time()) - int(timeseries[-1][0]) >= ALERT_ON_STALE_PERIOD:
                add_to_alert_on_stale_metrics = True
        except:
            # @modified 20180816 -
            #                      Feature #2492: alert on stale metrics
            add_to_alert_on_stale_metrics = False
        try:
            if int(time()) - int(timeseries[-1][0]) >= STALE_PERIOD:
                add_to_alert_on_stale_metrics = False
        except:
            add_to_alert_on_stale_metrics = False

        if add_to_alert_on_stale_metrics:
            try:
                redis_conn.ping()
            except:
                from redis import StrictRedis
                if REDIS_PASSWORD:
                    redis_conn = StrictRedis(password=REDIS_PASSWORD, unix_socket_path=REDIS_SOCKET_PATH)
                else:
                    redis_conn = StrictRedis(unix_socket_path=REDIS_SOCKET_PATH)
            try:
                redis_conn.sadd('analyzer.alert_on_stale_metrics', metric_name)
            except:
                pass

    # Get rid of short series
    if len(timeseries) < MIN_TOLERABLE_LENGTH:
        raise TooShort()

    # Get rid of stale series
    if time() - timeseries[-1][0] > STALE_PERIOD:
        raise Stale()

    # Get rid of boring series
    if len(set(item[1] for item in timeseries[-MAX_TOLERABLE_BOREDOM:])) == BOREDOM_SET_SIZE:
        raise Boring()

    # @added 20200117 - Feature #3400: Identify air gaps in the metric data
    # @modified 20200214 - Bug #3448: Repeated airgapped_metrics
    #                      Feature #3400: Identify air gaps in the metric data
    # if IDENTIFY_AIRGAPS:
    if IDENTIFY_AIRGAPS or IDENTIFY_UNORDERED_TIMESERIES:
        # airgaps = identify_airgaps(metric_name, timeseries, airgapped_metrics)
        # if airgaps:
        process_metric = True
        if IDENTIFY_AIRGAPS:
            if CHECK_AIRGAPS:
                metric_namespace_elements = metric_name.split('.')
                process_metric = False
                try:
                    for to_check in CHECK_AIRGAPS:
                        if to_check in metric_name:
                            process_metric = True
                            break
                        to_check_namespace_elements = to_check.split('.')
                        elements_matched = set(metric_namespace_elements) & set(to_check_namespace_elements)
                        if len(elements_matched) == len(to_check_namespace_elements):
                            process_metric = True
                            break
                except:
                    pass
            # Allow to skip identifying airgaps on certain metrics and namespaces
            if process_metric:
                metric_namespace_elements = metric_name.split('.')
                for to_skip in SKIP_AIRGAPS:
                    if to_skip in metric_name:
                        process_metric = False
                        break
                    to_skip_namespace_elements = to_skip.split('.')
                    elements_matched = set(metric_namespace_elements) & set(to_skip_namespace_elements)
                    if len(elements_matched) == len(to_skip_namespace_elements):
                        process_metric = False
                        break
        else:
            # If IDENTIFY_AIRGAPS is not enabled and
            # IDENTIFY_UNORDERED_TIMESERIES is enabled process the metric
            if IDENTIFY_UNORDERED_TIMESERIES:
                process_metric = True
        airgaps = None
        unordered_timeseries = False
        if process_metric:
            airgaps, unordered_timeseries = identify_airgaps(metric_name, timeseries, airgapped_metrics)
        if airgaps or unordered_timeseries:
            try:
                redis_conn.ping()
            except:
                from redis import StrictRedis
                if REDIS_PASSWORD:
                    redis_conn = StrictRedis(password=REDIS_PASSWORD, unix_socket_path=REDIS_SOCKET_PATH)
                else:
                    redis_conn = StrictRedis(unix_socket_path=REDIS_SOCKET_PATH)
        if airgaps:
            for i in airgaps:
                try:
                    redis_conn.sadd('analyzer.airgapped_metrics', str(i))
                    del airgaps
                    # TODO: learn_airgapped_metrics
                except:
                    pass
        # @added 20200214 - Bug #3448: Repeated airgapped_metrics
        #                   Feature #3400: Identify air gaps in the metric data
        # Also add unordered time series to the analyzer.unordered_timeseries
        # Redis set
        if unordered_timeseries:
            try:
                redis_conn.sadd('analyzer.unordered_timeseries', metric_name)
                del unorder_timeseries
            except:
                pass

    # RUN_OPTIMIZED_WORKFLOW - replaces the original ensemble method:
    # ensemble = [globals()[algorithm](timeseries) for algorithm in ALGORITHMS]
    # which runs all timeseries through all ALGORITHMS
    final_ensemble = []
    number_of_algorithms_triggered = 0
    number_of_algorithms_run = 0
    number_of_algorithms = len(ALGORITHMS)
    maximum_false_count = number_of_algorithms - CONSENSUS + 1
    # logger.info('the maximum_false_count is %s, above which CONSENSUS cannot be achieved' % (str(maximum_false_count)))
    consensus_possible = True
    # DEVELOPMENT: this is for a development version of analyzer only
    if skyline_app == 'analyzer_dev':
        time_all_algorithms = True
    else:
        time_all_algorithms = False

    algorithm_tmp_file_prefix = '%s/%s.' % (SKYLINE_TMP_DIR, skyline_app)

    for algorithm in ALGORITHMS:
        if consensus_possible:

            if send_algorithm_run_metrics:
                algorithm_count_file = '%s%s.count' % (algorithm_tmp_file_prefix, algorithm)
                algorithm_timings_file = '%s%s.timings' % (algorithm_tmp_file_prefix, algorithm)

            run_algorithm = []
            run_algorithm.append(algorithm)
            number_of_algorithms_run += 1
            if send_algorithm_run_metrics:
                start = timer()
            try:
                algorithm_result = [globals()[test_algorithm](timeseries) for test_algorithm in run_algorithm]
            except:
                # logger.error('%s failed' % (algorithm))
                algorithm_result = [None]

            if send_algorithm_run_metrics:
                end = timer()
                with open(algorithm_count_file, 'a') as f:
                    f.write('1\n')
                with open(algorithm_timings_file, 'a') as f:
                    f.write('%.6f\n' % (end - start))
        else:
            algorithm_result = [False]
            # logger.info('CONSENSUS NOT ACHIEVABLE - skipping %s' % (str(algorithm)))

        if algorithm_result.count(True) == 1:
            result = True
            number_of_algorithms_triggered += 1
            # logger.info('algorithm %s triggerred' % (str(algorithm)))
        elif algorithm_result.count(False) == 1:
            result = False
        elif algorithm_result.count(None) == 1:
            result = None
        else:
            result = False

        final_ensemble.append(result)

        if not RUN_OPTIMIZED_WORKFLOW:
            continue

        if time_all_algorithms:
            continue

        if ENABLE_ALL_ALGORITHMS_RUN_METRICS:
            continue

        # true_count = final_ensemble.count(True)
        # false_count = final_ensemble.count(False)
        # logger.info('current false_count %s' % (str(false_count)))

        if final_ensemble.count(False) >= maximum_false_count:
            consensus_possible = False
            # logger.info('CONSENSUS cannot be reached as %s algorithms have already not been triggered' % (str(false_count)))
            # skip_algorithms_count = number_of_algorithms - number_of_algorithms_run
            # logger.info('skipping %s algorithms' % (str(skip_algorithms_count)))

    # logger.info('final_ensemble: %s' % (str(final_ensemble)))

    try:
        # ensemble = [globals()[algorithm](timeseries) for algorithm in ALGORITHMS]
        ensemble = final_ensemble

        threshold = len(ensemble) - CONSENSUS
        if ensemble.count(False) <= threshold:
            if ENABLE_SECOND_ORDER:
                if is_anomalously_anomalous(metric_name, ensemble, timeseries[-1][1]):
                    return True, ensemble, timeseries[-1][1]
            else:
                return True, ensemble, timeseries[-1][1]

        return False, ensemble, timeseries[-1][1]
    except:
        logger.error('Algorithm error: %s' % traceback.format_exc())
        return False, [], 1
