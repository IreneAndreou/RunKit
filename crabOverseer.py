import datetime
import json
import os
import select
import shutil
import sys
import yaml

if __name__ == "__main__":
  file_dir = os.path.dirname(os.path.abspath(__file__))
  sys.path.append(os.path.dirname(file_dir))
  __package__ = 'RunKit'

from .crabTaskStatus import JobStatus, Status, StatusOnServer, StatusOnScheduler
from .crabTask import Task
from .sh_tools import sh_call

class TaskStat:
  summary_only_thr = 10

  def __init__(self):
    self.all_tasks = []
    self.tasks_by_status = {}
    self.n_jobs = 0
    self.total_job_stat = {}
    self.max_job_stat = {}
    self.unknown = []
    self.waiting_for_recovery = []
    self.failed = []

  def add(self, task):
    self.all_tasks.append(task)
    if task.taskStatus.status not in self.tasks_by_status:
      self.tasks_by_status[task.taskStatus.status] = []
    self.tasks_by_status[task.taskStatus.status].append(task)
    if task.taskStatus.status == Status.InProgress:
      for job_status, count in task.taskStatus.job_stat.items():
        if job_status not in self.total_job_stat:
          self.total_job_stat[job_status] = 0
        self.total_job_stat[job_status] += count
        self.n_jobs += count
        if job_status not in self.max_job_stat or self.max_job_stat[job_status][0] < count:
          self.max_job_stat[job_status] = (count, task)
    if task.taskStatus.status == Status.Unknown:
      self.unknown.append(task)
    if task.taskStatus.status == Status.WaitingForRecovery:
      self.waiting_for_recovery.append(task)
    if task.taskStatus.status == Status.Failed:
      self.failed.append(task)


  def report(self):
    status_list = sorted(self.tasks_by_status.keys(), key=lambda x: x.value)
    n_tasks = len(self.all_tasks)
    status_list = [ f"{n_tasks} Total" ] + [ f"{len(self.tasks_by_status[x])} {x.name}" for x in status_list ]
    print('Tasks: ' + ', '.join(status_list))
    job_stat = [ f"{self.n_jobs} total" ] + \
               [ f'{cnt} {x.name}' for x, cnt in sorted(self.total_job_stat.items(), key=lambda a: a[0].value) ]
    if self.n_jobs > 0:
      print('Jobs in active tasks: ' + ', '.join(job_stat))

    if len(self.tasks_by_status[Status.InProgress]) > TaskStat.summary_only_thr:
      if(len(self.max_job_stat.items())):
        print('Task with ...')
        for job_status, (cnt, task) in sorted(self.max_job_stat.items(), key=lambda a: a[0].value):
          print(f'\tmax {job_status.name} jobs = {cnt}: {task.name} {task.taskStatus.dashboard_url}')
    else:
      for task in self.tasks_by_status[Status.InProgress]:
        text = f'{task.name}: status={task.taskStatus.status.name}. '
        job_info = []
        for job_status, count in sorted(task.taskStatus.job_stat.items(), key=lambda x: x[0].value):
          job_info.append(f'{count} {job_status.name}')
        if len(job_info) > 0:
          text += ', '.join(job_info) + '. '
        if task.taskStatus.dashboard_url is not None:
          text += task.taskStatus.dashboard_url
        print(text)
    if len(self.unknown) > 0:
      print('Tasks with unknown status:')
      for task in self.unknown:
        print(f'{task.name}: {task.taskStatus.parse_error}. {task.lastCrabStatusLog()}')
    if len(self.waiting_for_recovery) > 0:
      names = [ task.name for task in self.waiting_for_recovery ]
      print(f"Tasks waiting for recovery: {', '.join(names)}")
    if len(self.failed) > 0:
      names = [ task.name for task in self.failed ]
      print(f"Failed tasks that require manual intervention: {', '.join(names)}")

def timestamp_str():
  t = datetime.datetime.now()
  t_str = t.strftime('%Y-%m-%d %H:%M:%S')
  return f'[{t_str}] '

def sanity_checks(task):
  if task.taskStatus.status == Status.InProgress:
    job_states = sorted(task.taskStatus.job_stat.keys(), key=lambda x: x.value)
    ref_states = [ JobStatus.running, JobStatus.finished, JobStatus.failed ]
    if len(job_states) <= len(ref_states) and job_states == ref_states[:len(job_states)]:
      now = datetime.datetime.now()
      start_times = task.taskStatus.get_detailed_job_stat('StartTimes', JobStatus.running)
      abnormal_run_thr = 24
      job_runs = []
      for job_id, start_time in start_times.items():
        t = datetime.datetime.fromtimestamp(start_time[-1])
        delta_t = (now - t).total_seconds() / (60 * 60)
        job_runs.append([job_id, delta_t])
      job_runs = sorted(job_runs, key=lambda x: x[1])
      max_run = job_runs[0][1]
      if max_run > abnormal_run_thr:
        text = f'{task.name}: all running jobs are running for at least {max_run:.1f} hours.' \
              + ' It is very likely that these jobs are stacked. Task will be killed following by recovery attempts.'
        print(text)
        task.kill()
        return False
  return True

def update(tasks, no_status_update=False):
  print(timestamp_str() + "Updating...")
  stat = TaskStat()
  to_post_process = []
  for task_name, task in tasks.items():
    if task.taskStatus.status == Status.Defined:
      task.submit()
    elif task.taskStatus.status.value < Status.CrabFinished.value:
      if task.taskStatus.status.value < Status.WaitingForRecovery.value and not no_status_update:
        task.updateStatus()
      if task.taskStatus.status == Status.WaitingForRecovery:
        if task.taskStatus.status_on_server == StatusOnServer.SUBMITFAILED:
          task.recoverLocal()
        elif task.taskStatus.status_on_scheduler in [ StatusOnScheduler.FAILED, StatusOnScheduler.FAILED_KILLED ] \
              or not task.resubmit():
          task.recover()
      else:
        if task.hasFailedJobs():
          task.resubmit()
    sanity_checks(task)
    if task.taskStatus.status == Status.CrabFinished:
      if task.checkCompleteness():
        task.preparePostProcessLists()
        done_flag = task.getPostProcessingDoneFlagFile()
        if os.path.exists(done_flag):
          os.remove(done_flag)
        to_post_process.append(task)
      else:
        task.taskStatus.status = Status.Failed
        task.saveStatus()
    stat.add(task)
  stat.report()
  return to_post_process

def overseer_main(work_area, cfg_file, new_task_list_files, verbose=1, no_status_update=False,
                  update_cfg=False, no_loop=False):
  if not os.path.exists(work_area):
    os.makedirs(work_area)
  abs_work_area = os.path.abspath(work_area)
  cfg_path = os.path.join(work_area, 'cfg.yaml')
  if cfg_file is not None:
    shutil.copyfile(cfg_file, cfg_path)
  if not os.path.isfile(cfg_path):
    raise RuntimeError("The overseer configuration is not found")
  with open(cfg_path, 'r') as f:
    main_cfg = yaml.safe_load(f)

  task_list_path = os.path.join(work_area, 'tasks.json')
  tasks = {}
  if os.path.isfile(task_list_path):
    with open(task_list_path, 'r') as f:
      task_names = json.load(f)
      for task_name in task_names:
        tasks[task_name] = Task.Load(mainWorkArea=work_area, taskName=task_name)
  for task_list_file in new_task_list_files:
    with open(task_list_file, 'r') as f:
      new_tasks = yaml.safe_load(f)
    for task_name in new_tasks:
      if task_name == 'config': continue
      if task_name in tasks:
        if update_cfg:
          tasks[task_name].updateConfig(main_cfg, new_tasks)
      else:
        tasks[task_name] = Task.Create(work_area, main_cfg, new_tasks, task_name)
  with open(task_list_path, 'w') as f:
    json.dump([task_name for task_name in tasks], f, indent=2)

  for name, task in tasks.items():
    task.checkConfigurationValidity()

  if 'updateInterval' in main_cfg:
    update_interval = main_cfg['updateInterval']
  else:
    update_interval = 60

  while True:
    last_update = datetime.datetime.now()
    to_post_process = update(tasks, no_status_update=no_status_update)
    if len(to_post_process) > 0 and 'postProcessing' in main_cfg:
      print(timestamp_str() + "Post-processing: " + ', '.join([ task.name for task in to_post_process ]))
      postproc_params = main_cfg['postProcessing']
      cmd = [ 'law', 'run', postproc_params['lawTask'],
              '--workflow', postproc_params['workflow'],
              '--bootstrap-path', postproc_params['bootstrap'],
              '--work-area', abs_work_area,
              '--log-path', os.path.join(abs_work_area, 'law', 'logs'),
              '--sub-dir', os.path.join(abs_work_area, 'law', 'jobs') ]
      if 'requirements' in postproc_params:
        cmd.extend(['--requirements', postproc_params['requirements']])
      sh_call(cmd)
      for task in to_post_process:
        task.updateStatusFromFile()
      print(timestamp_str() + "Post-processing iteration finished.")
    has_unfinished = False
    for task_name, task in tasks.items():
      if not ((task.taskStatus.status == Status.CrabFinished and 'postProcessing' not in main_cfg) \
              or task.taskStatus.status == Status.PostProcessingFinished \
              or task.taskStatus.status == Status.Failed ):
        has_unfinished = True
        break

    if no_loop or not has_unfinished: break
    delta_t = (datetime.datetime.now() - last_update).total_seconds() / 60
    to_sleep = int(update_interval - delta_t)
    if to_sleep >= 1:
      print(f"\n{timestamp_str()}Waiting for {to_sleep} minutes until the next update. Press return to exit.")
      rlist, wlist, xlist = select.select([sys.stdin], [], [], to_sleep * 60)
      if rlist:
        print(timestamp_str() + "Exiting...")
        break
    sh_call(['kinit', '-R'])
  if not has_unfinished:
    print("All tasks are done.")

if __name__ == "__main__":
  import argparse
  parser = argparse.ArgumentParser(description='CRAB overseer.')
  parser.add_argument('--work-area', required=False, type=str, default='.crabOverseer',
                      help="Working area to store overseer and crab tasks states")
  parser.add_argument('--cfg', required=False, type=str, default=None, help="configuration file")
  parser.add_argument('--no-status-update', action="store_true", help="Do not update tasks statuses.")
  parser.add_argument('--update-cfg', action="store_true", help="Update task configs.")
  parser.add_argument('--no-loop', action="store_true", help="Run task update once and exit.")
  parser.add_argument('--verbose', required=False, type=int, default=1, help="verbosity level")
  parser.add_argument('task_file', type=str, nargs='*', help="file(s) with task descriptions")
  args = parser.parse_args()

  overseer_main(args.work_area, args.cfg, args.task_file, verbose=args.verbose, no_status_update=args.no_status_update,
                update_cfg=args.update_cfg)