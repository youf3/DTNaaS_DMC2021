import requests, time
from prometheus_http_client import Prometheus
import pandas as pd
import numpy as np
import logging
from http.client import HTTPException
import json

AVG_INT = 15
STEP = 15
MAX_RES = 11000

class DTN(object):
    def __init__(self, name, man_addr, data_addr, mon_addr, interface):
        self.name = name
        self.man_addr = man_addr
        self.data_addr = data_addr
        self.interface = interface
        self.mon_addr = mon_addr

# register DTN to orchestrator
def add_dtn_to_orchestrator(sender, receiver, orchestrator):

    data = {
        'name': 'receiver',
        'man_addr': receiver.man_addr,
        'data_addr': receiver.data_addr,
        'username': 'nobody',
        'interface': receiver.interface
    }
    # register receiver and get ID 1
    response = requests.post('{}/DTN/'.format(orchestrator), json=data)    
    result = response.json()
    assert result == {'id': 1}
    
    data = {
        'name': 'sender',
        'man_addr': sender.man_addr,
        'data_addr': sender.data_addr,
        'username': 'nobody',        
        'interface': sender.interface
    }
    # register sender and get ID 2
    response = requests.post('{}/DTN/'.format(orchestrator), json=data)
    result = response.json()
    assert result == {'id': 2}
    

# check latency between DTNs
def test_ping(orchestrator):
    response = requests.get('{}/ping/2/1'.format(orchestrator))
    result = response.json()
    print(result)


# Start Transfer from sender to receiver on /data directory
def test_transfer(sender, receiver, orchestrator, num_workers = 8):   
    # retrieve files from sender 
    result =  requests.get('http://{}/files/'.format(sender.man_addr))    

    # classify files and dirs from returned list
    files = result.json()
    file_list = [i['name'] for i in files if i['type'] == 'file'][:5]
    dirs = [i['name'] for i in files if i['type'] == 'dir']

    # create dirs in receiver
    response = requests.post('http://{}/create_dir/'.format(receiver.man_addr),json=dirs)
    if response.status_code != 200: raise Exception('failed to create dirs')

    # prepare files to send 
    data = {
        # list of files to send
        'srcfile' : file_list,
        # list of files to receive
        'dstfile' : file_list,
        # number of simultaneous connections
        'num_workers' : num_workers,
        # block size to use
        'blocksize' : 8192,
        # disable zero-copy
        'zerocopy' : False,
    }

    # start transfer using nuttcp
    response = requests.post('{}/transfer/nuttcp/2/1'.format(orchestrator),json=data) 
    result = response.json()
    assert result['result'] == True
    transfer_id = result['transfer']
    print('transfer_id %s' % (transfer_id))
    return transfer_id

# wait for transfer to finish
def finish_transfer(transfer_id, orchestrator, sender, receiver):    
    response = requests.post('{}/wait/{}'.format(orchestrator, transfer_id))
    result = response.json()   

    cleanup(sender, receiver)

# clean up DTNs after transfer
def cleanup(sender, receiver, retry = 5):

    for i in range(0, retry):        
        response = requests.get('http://{}/cleanup/nuttcp'.format(sender.man_addr))
        if response.status_code != 200: continue
        response = requests.get('http://{}/cleanup/nuttcp'.format(receiver.man_addr))
        if response.status_code != 200: continue        
        
        return 
    raise Exception('Cannot cleanup after %s tries' % retry)

# wait for transfer to finish
def wait_for_transfer(transfer_id, orchestrator, sender):
    while True:
        response = requests.get('{}/check/{}'.format(orchestrator, transfer_id))
        result = response.json()
        if result['Unfinished'] == 0:
            response = requests.get('http://{}/cleanup/nuttcp'.format(sender.man_addr))
            break
        time.sleep(30)

# mark transfer to finished
def finish_transfer(transfer_id, orchestrator, sender, receiver):    
    response = requests.post('{}/wait/{}'.format(orchestrator, transfer_id))
    result = response.json()

    cleanup(sender, receiver)

# get transfer detail
def get_transfer(transfer_id, orchestrator):
    
    response = requests.get('{}/transfer/{}'.format(orchestrator, transfer_id))
    result = response.json()
    print(result)
    return result

# send data query to DTNs
def send_query(query, start, end, step, url):
    
    prometheus = Prometheus()
    prometheus.url = url

    res = prometheus.query_rang(metric=query, start=start, end=end, step=step)    
    return res

# remove unnecessary header for dataset
def prettify_header(metric):
    metrics_to_remove = ['instance', 'job', 'mode', '__name__', 'container', 'endpoint', 'namespace', 'pod', 'prometheus', 'service']
    for i in metrics_to_remove:
        if i in metric: del metric[i]
    if len(metric) > 1 : raise Exception('too many metric labels')
    else:
        return next(iter(metric.keys()))

# extract data from monitoring system
def extractor(sender, receiver, start_time, end_time, monitor_url):
    AVG_INT = 15        
    query = (
    'label_replace(sum by (instance)(irate(node_network_transmit_bytes_total{{instance=~"{4}.*", device="{2}"}}[{1}m])), "network_throughput", "$0", "instance", "(.+)") '
    'or label_replace(sum by (job)(irate(node_disk_written_bytes_total{{instance=~"{5}.*", device=~"nvme.*"}}[{1}m])),"Goodput", "$0", "job", "(.+)") '        
    'or label_replace(sum by (job)(1 - irate(node_cpu_seconds_total{{mode="idle", instance="{4}"}}[1m])),"CPU", "$0", "job", "(.+)") '
    'or label_replace(max by (container)(container_memory_working_set_bytes{{namespace="{3}", container=~"{0}.*"}}), "Memory_used", "$0", "container", "(.+)") '
    'or label_replace(node_memory_Active_bytes{{instance="{4}"}}, "Memory_used", "$0", "instance", "(.+)") '    
    'or label_replace(sum by (job)(irate(node_disk_read_bytes_total{{instance=~"{4}.*", device=~"nvme.*"}}[{1}m])),"NVMe_transfer_bytes", "$0", "job", "(.+)") '
    'or label_replace(sum by (job)(irate(node_disk_io_time_seconds_total{{instance=~"{4}.*", device=~"nvme.*"}}[{1}m])),"NVMe_total_util", "$0", "job", "(.+)") '    
    'or label_replace(count by (job)(node_disk_io_time_seconds_total{{instance=~"{4}.*", device=~"nvme[0-7]n1"}}),"Storage_count", "$0", "job", "(.+)") '
    'or label_replace(sum by (job)(node_network_speed_bytes{{instance=~"{4}.*", device="{2}"}} * 8), "NIC_speed", "$0", "job", "(.+)") '
    'or label_replace(sum by (job)(irate(node_netstat_Tcp_RetransSegs{{instance=~"{4}.*"}}[{1}m])), "Packet_losses", "$0", "job", "(.+)") '
    '').format(sender.name, AVG_INT, sender.interface, 'dtnaas', sender.mon_addr, receiver.mon_addr)

    dataset = None
    
    while end_time > start_time:        
        data_in_period = None
        max_ts = start_time + (STEP * MAX_RES) 
        next_hop_ts = end_time if max_ts > end_time else max_ts
        logging.debug('Getting data for {} : {}'.format(start_time, end_time))
        res = send_query(query, start_time, next_hop_ts, STEP, monitor_url)
        if '401 Authorization Required' in res: raise HTTPException(res)
        response = json.loads(res)
        if response['status'] != 'success': raise Exception('Failed to query Prometheus server')
        
        for result in response['data']['result']:
            result['metric'] = prettify_header(result['metric'])            
            df = pd.DataFrame(data=result['values'], columns = ['Time', result['metric']], dtype=float)            
            df['Time'] = pd.to_datetime(df['Time'], unit='s')
            df.set_index('Time', inplace=True)

            data_in_period = df if data_in_period is None else data_in_period.merge(df, how='outer',  on='Time').sort_index()
        
        dataset = data_in_period if dataset is None else dataset.append(data_in_period)
        start_time = next_hop_ts

    cols = dataset.columns.tolist()
    labels_to_rearrange = ['NVMe_total_util', 'NVMe_transfer_bytes']    
    for i in labels_to_rearrange: 
        cols.remove(i)
        cols.insert(0,i)    
    
    return dataset[cols]

# define DTN and monitor information
sender = DTN('Ciena_Ottawa','162.244.229.51:5000', '74.114.96.105', '162.244.229.51:9100',  'enp175s0f0.555')
receiver = DTN('r740xd1', '165.124.33.174:5000', '74.114.96.98', '165.124.33.174:9100', 'vlan555')
orchestrator = 'http://162.244.229.51:5002'
monitor = "http://165.124.33.158:9091"

# DTNs already added
# add_dtn_to_orchestrator(sender,receiver,orchestrator)

# check latency
test_ping(orchestrator)

# start transfer
transfer_id = test_transfer(sender, receiver, orchestrator)

# waiting for transfer to finish
wait_for_transfer(transfer_id, orchestrator, sender)

# mark transfer to finishec
finish_transfer(transfer_id, orchestrator, sender, receiver)

# get transfer detail
transfer_detail = get_transfer(transfer_id, orchestrator)

# use the transfer detail to query dataset
df = extractor(sender, receiver, transfer_detail['start_time'], transfer_detail['end_time'], monitor)
print(df)