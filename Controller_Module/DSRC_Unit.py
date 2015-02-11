__author__ = 'xuepeng'

import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import math
import DSRC_Event
import DSRC_JobProcessor
import time
import thread

from DSRC_Event import USRPEventHandler, EventListener, Event
from DSRC_USRP_Connector import DsrcUSRPConnector
from DSRC_JobProcessor import JobProcessor, Job, JobCallback
from iRobot_Module.create import Create
from threading import Thread, Lock


DSRC_UNIT_MODE_LEAD = 1
DSRC_UNIT_MODE_FOLLOW = 2
DSRC_UNIT_MODE_FREE = 3

DSRC_POSITION_UPDATE_INTERVAL = 0.05


class DSRCUnit(EventListener, JobCallback):
    def __init__(self, unit_id, socket_port=10123, robot_port="/dev/ttyUSB0", unit_mode=DSRC_UNIT_MODE_FREE,
                 avoid_collision_mode=False):
        """
        :param unit_id: The ID of the car unit
        :param socket_port: The port number of the socket, which is used to connect USRP module.
        :param robot_port: The USB port for the iRobot
        :param unit_mode: The mode of the car unit
        :param avoid_collision_mode: Emergency mode.
        """
        self.unit_id = unit_id
        self.socket_port = socket_port
        self.robot_port = robot_port
        self.unit_mode = unit_mode
        self.avoid_collision_mode = avoid_collision_mode

        # Handle the message from USRP, and generate event
        self.USRP_event_handler = USRPEventHandler()
        self.USRP_event_handler.set_listener(listener=self)
        # The connector between USRP and Controller module
        self.USRP_connect = DsrcUSRPConnector(self.socket_port, self.USRP_event_handler)
        # iRobot
        self.create = Create(self.robot_port)
        # self.create = None
        # A processor to process the robot job in order
        self.job_processor = JobProcessor(self.create)
        self.position_tracker = DSRCPositionTracker(self.job_processor, 0, 0, 0)
        self.position_tracker.start()
        self.car_info()

    def set_unit_mode(self, mode):
        self.unit_mode = mode

    def set_avoid_collision(self, isAvoid):
        self.avoid_collision_mode = isAvoid

    def car_info(self):
        print "Car Unit:" + self.unit_id

    def usrp_event_received(self, event):
        if event.source == self.unit_id:
            return
        if event.destination in (DSRC_Event.DESTINATION_ALL, self.unit_id):
            if event.type == DSRC_Event.TYPE_CAR_CAR:
                action = event.action
                coordinates = event.coordinates
                print "Action:" + action.name + ":" + str(action.arg1) + ":" + str(action.arg2)
                print "Coordinates:" + str(coordinates.x) + ":" + str(coordinates.y) + ":" + str(coordinates.radian)
                # TODO: collision detection
                if self.unit_mode == DSRC_UNIT_MODE_FOLLOW:
                    new_job = Job(jobCallback=self, action=action.name, arg1=action.arg1, arg2=action.arg2, time=0)
                    self.job_processor.add_new_job(new_job)

            elif event.type == DSRC_Event.TYPE_MONITOR_CAR:
                # TODO:
                pass

    def irobot_event_received(self, event):
        # TODO:
        print "iRobot event functionality is not yet implemented!"

    def job_finished(self, action, arg1, arg2, timeExecuted):
        if action == DSRC_JobProcessor.GO:
            self.position_tracker.update_primary(arg1, arg2, timeExecuted)

    def job_paused(self, action, arg1, arg2, timeExecuted):
        if action == DSRC_JobProcessor.GO:
            self.position_tracker.update_primary(arg1, arg2, timeExecuted)

    def stop_self(self):
        self.job_processor.stop_processor()
        self.position_tracker.stop_self()
        self.USRP_event_handler.stop_self()


# TODO: Create a position calculation looper with 0.01s interval. The position can be classified into two different types
# TODO: based on accuracy. The looper calculate the secondary position, while the job event can calculate primary position

class DSRCPositionTracker(Thread):
    def __init__(self, processor, x=0, y=0, radian=0):
        """
        :param processor: JobProcessor
        :param x: x coordinate, in cm
        :param y: y coordinate, in cm
        :param radian: the direction in which the car facing
        """
        Thread.__init__(self)
        self.processor = processor
        # secondary position
        self.x = x
        self.y = y
        self.radian = radian
        # primary position
        self._x = x
        self._y = y
        self._radian = radian
        # primary updated
        self.primary_updated = True
        self.running = True
        self.pos_lock = thread.allocate_lock()

    def _update_secondary(self):
        if not self.processor.pause:
            job = self.processor.currentJob
            if job:
                if job.action == DSRC_JobProcessor.GO:
                    with self.pos_lock:
                        self._calculate_secondary_position(job.arg1, job.arg2, DSRC_POSITION_UPDATE_INTERVAL)
        self.primary_updated = False

    #TODO: test the method
    def _calculate_secondary_position(self, arg1, arg2, arg_time):
        if arg1 == 0:
            # if the velocity is 0, calculate the radians by the multiplication of angular velocity and time
            radian_per_sec = math.radians(arg2)
            radian = radian_per_sec * arg_time
            self.radian = (self.radian + radian) % (2 * math.pi)
        elif arg2 == 0:
            # if angular velocity is 0, calculate the x velocity and y velocity
            # and then the distance moved in both x and y
            x_velocity = arg1 * math.cos(self.radian)
            y_velocity = arg1 * math.sin(self.radian)
            self.x += x_velocity * arg_time
            self.y += y_velocity * arg_time
        else:
            radian_pos = {'x': self.x, 'y': self.y, 'radian': self.radian}
            new_radian_pos = DSRCPositionTracker.calculate_pos(radian_pos, arg1, arg2, arg_time)
            self.x = new_radian_pos['x']
            self.y = new_radian_pos['y']
            self.radian = new_radian_pos['radian']
        print "Position:"+str(self.x) + ":" + str(self.y) + ":" + str(self.radian)

    @staticmethod
    def calculate_pos(radian_pos, arg1, arg2, arg_time):
        """
        :param radian_pos: radian, x and y. radian_pos['x'] is x, radian_pos['y'] is y, radian_pos['radian'] is radian
        :param arg1: velocity
        :param arg2: angular velocity
        :param arg_time: execution time
        :return: new radian_pos with the same format
        """
        radian_per_sec = math.radians(arg2)
        radius = arg1 / radian_per_sec
        radian = radian_per_sec * arg_time
        # create polar coordinate system with current point as origin and the direction of velocity as x axis
        # The distance between new point and current point is
        l = 2 * radius * math.sin(radian/2)
        # The angle is
        theta = radian/2

        # Do a coordinate transformation
        new_theta = theta + radian_pos['radian']
        #     Polar system to cartesian system
        x = l * math.cos(new_theta)
        y = l * math.sin(new_theta)
        new_x = x + radian_pos['x']
        new_y = y + radian_pos['y']
        new_radian = ((radian + radian_pos['radian']) % (2 * math.pi))
        new_radian_pos = {'x': new_x, 'y': new_y, 'radian': new_radian}
        return new_radian_pos

    def run(self):
        while self.running:
            self._update_secondary()
            time.sleep(DSRC_POSITION_UPDATE_INTERVAL)


    def update_primary(self, arg1, arg2, arg_time):
        with self.pos_lock:
            if arg1 == 0:
                # if the velocity is 0, calculate the radians by the multiplication of angular velocity and time
                radian_per_sec = math.radians(arg2)
                radian = radian_per_sec * arg_time
                self._radian = self.radian = ((self._radian + radian) % (2 * math.pi))
            elif arg2 == 0:
                # if angular velocity is 0, calculate the x velocity and y velocity
                # and then the distance moved in both x and y
                x_velocity = arg1 * math.cos(self.radian)
                y_velocity = arg1 * math.sin(self.radian)
                self.x = self._x = (self._x + x_velocity * arg_time)
                self.y = self._y = (self._y + y_velocity * arg_time)
            else:
                radian_pos = {'x': self._x, 'y': self._y, 'radian': self._radian}
                new_radian_pos = DSRCPositionTracker.calculate_pos(radian_pos, arg1, arg2, arg_time)
                self._x = self.x = new_radian_pos['x']
                self._y = self.y = new_radian_pos['y']
                self._radian = self.radian = new_radian_pos['radian']
                self.primary_updated = True
        print "Position:"+str(self.x) + ":" + str(self.y) + ":" + str(self.radian)

    def stop_self(self):
        self.running = False


def test_position():
    unit = DSRCUnit("car1")
    job1 = Job(unit, DSRC_JobProcessor.GO, 8, 30, 45)
    job2 = Job(unit, DSRC_JobProcessor.GO, 1, 0, 90)
    job3 = Job(unit, DSRC_JobProcessor.GO, 1, 0, -90)
    job4 = Job(unit, DSRC_JobProcessor.GO, 1, 30, 0)
    job5 = Job(unit, DSRC_JobProcessor.GO, 0, 0, 0)
    job6 = Job(unit, DSRC_JobProcessor.GO, 3, -20, 0)
    job7 = Job(unit, DSRC_JobProcessor.GO, 0, 0, 0)
    unit.job_processor.add_new_job(job1)
    unit.job_processor.add_new_job(job2)
    unit.job_processor.add_new_job(job3)
    unit.job_processor.add_new_job(job4)
    unit.job_processor.add_new_job(job5)
    unit.job_processor.add_new_job(job6)
    unit.job_processor.add_new_job(job7)
    while True:
        s = raw_input()
        if s == 'q':
            break
        elif s == 'p':
            unit.job_processor.pause_processor()
        elif s == 'r':
            unit.job_processor.resume_processor()
        elif s == 's':
            unit.stop_self()
        elif s == 'ir':
            job8 = Job(unit, DSRC_JobProcessor.GO, 1, 0, 90)
            unit.job_processor.insert_new_job(job8)

def test_follow_mode():
    unit = DSRCUnit("car1")
    unit.set_unit_mode(DSRC_UNIT_MODE_FOLLOW)
    while True:
        s = raw_input()
        if s == 'q':
            unit.stop_self()
            break

def main():
    pass

if __name__ == '__main__':
    test_follow_mode()