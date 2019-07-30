#!/usr/bin/env python

# Copyright (c) 2017 Computer Vision Center (CVC) at the Universitat Autonoma de
# Barcelona (UAB).
#
# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.

# Allows controlling a vehicle with a keyboard.

"""
Welcome to CARLA manual control.

Use ARROWS or WASD keys for control.

    W            : throttle
    S            : brake
    AD           : steer
    Q            : toggle reverse
    Space        : hand-brake
    P            : toggle autopilot

    TAB          : change sensor position
    `            : next sensor
    [1-9]        : change to sensor [1-9]
    C            : change weather (Shift+C reverse)
    Backspace    : change vehicle

    R            : toggle recording images to disk

    F1           : toggle HUD
    H/?          : toggle help
    ESC          : quit
"""

from __future__ import print_function

# ==============================================================================
# -- imports -------------------------------------------------------------------
# ==============================================================================


import carla

from carla import ColorConverter as cc

import argparse
import collections
import datetime
import logging
import math
import re
import time
import weakref

try:
    import pygame
    from pygame.locals import KMOD_CTRL
    from pygame.locals import KMOD_SHIFT
    from pygame.locals import K_0
    from pygame.locals import K_9
    from pygame.locals import K_BACKQUOTE
    from pygame.locals import K_BACKSPACE
    from pygame.locals import K_DOWN
    from pygame.locals import K_ESCAPE
    from pygame.locals import K_F1
    from pygame.locals import K_LEFT
    from pygame.locals import K_RIGHT
    from pygame.locals import K_SLASH
    from pygame.locals import K_SPACE
    from pygame.locals import K_TAB
    from pygame.locals import K_UP
    from pygame.locals import K_a
    from pygame.locals import K_c
    from pygame.locals import K_d
    from pygame.locals import K_h
    from pygame.locals import K_p
    from pygame.locals import K_q
    from pygame.locals import K_r
    from pygame.locals import K_s
    from pygame.locals import K_w
except ImportError:
    raise RuntimeError('cannot import pygame, make sure pygame package is installed')

try:
    import numpy as np
except ImportError:
    raise RuntimeError('cannot import numpy, make sure numpy package is installed')

import csv
import random
import tables # in order to save data
import pandas as pd


# ==============================================================================
# -- World ---------------------------------------------------------------------
# ==============================================================================


def find_weather_presets():
    rgx = re.compile('.+?(?:(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|$)')
    name = lambda x: ' '.join(m.group(0) for m in rgx.finditer(x))
    presets = [x for x in dir(carla.WeatherParameters) if re.match('[A-Z].+', x)]
    # print("preset weathers", presets)
    # ['ClearNoon', 'ClearSunset', 'CloudyNoon', 'CloudySunset', 'Default', 'HardRainNoon', 'HardRainSunset', 'MidRainSunset', 'MidRainyNoon', 'SoftRainNoon', 'SoftRainSunset', 'WetCloudyNoon', 'WetCloudySunset', 'WetNoon', 'WetSunset']
    return [(getattr(carla.WeatherParameters, x), name(x)) for x in presets]


def get_actor_display_name(actor, truncate=250):
    name = ' '.join(actor.type_id.replace('_', '.').title().split('.')[1:])
    return (name[:truncate-1] + u'\u2026') if len(name) > truncate else name


class World(object):
    def __init__(self, carla_world, hud):
        self.world = carla_world
        settings = self.world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = 0.1 # must be smaller or equal to 0.1s
        # self.world.apply_settings(settings)

        self.mapname = carla_world.get_map().name
        self.hud = hud
        self.world.on_tick(hud.on_world_tick)
        self.world.wait_for_tick(10.0)
        self.vehicle = None
        while self.vehicle is None:
            print("Scenario not yet ready")
            time.sleep(1)
            possible_vehicles = self.world.get_actors().filter('vehicle.*')
            for vehicle in possible_vehicles:
                if vehicle.attributes['role_name'] == "hero":
                    self.vehicle = vehicle  # hero vehicle Actor(id=721, type=vehicle.lincoln.mkz2017)
        self.vehicle_name = self.vehicle.type_id
        self.collision_sensor = CollisionSensor(self.vehicle, self.hud)
        self.lane_invasion_sensor = LaneInvasionSensor(self.vehicle, self.hud)
        self.camera_manager = CameraManager(self.vehicle, self.hud) # depth camera is 20Hz
        # RH: the following line determines what kind of camera the vehicle is using
        self.camera_manager.set_sensor(3, notify=False) # 0 for rgb raw data, 3 for depth camera
        self.controller = None
        self._weather_presets = find_weather_presets()
        self._weather_index = 0

    def restart(self):
        cam_index = self.camera_manager._index
        cam_pos_index = self.camera_manager._transform_index
        start_pose = self.vehicle.get_transform()
        start_pose.location.z += 2.0
        start_pose.rotation.roll = 0.0
        start_pose.rotation.pitch = 0.0
        blueprint = self._get_random_blueprint()
        self.destroy()
        self.vehicle = self.world.spawn_actor(blueprint, start_pose)
        self.collision_sensor = CollisionSensor(self.vehicle, self.hud)
        self.lane_invasion_sensor = LaneInvasionSensor(self.vehicle, self.hud)
        self.camera_manager = CameraManager(self.vehicle, self.hud)
        self.camera_manager._transform_index = cam_pos_index
        self.camera_manager.set_sensor(cam_index, notify=False)
        actor_type = get_actor_display_name(self.vehicle)
        self.hud.notification(actor_type)

    def next_weather(self, reverse=False):
        self._weather_index += -1 if reverse else 1
        self._weather_index %= len(self._weather_presets)
        preset = self._weather_presets[self._weather_index]
        self.hud.notification('Weather: %s' % preset[1])
        self.vehicle.get_world().set_weather(preset[0])

    def tick(self, clock):
        if len(self.world.get_actors().filter(self.vehicle_name)) < 1:
            print("Scenario ended -- Terminating")
            return False

        # set all traffic lights to Green, it will still affect by one tick and not afterwards
        traffic_light = self.vehicle.get_traffic_light()
        if traffic_light is not None:
            traffic_light.set_state(carla.TrafficLightState.Green)

        self.hud.tick(self, self.mapname, clock)

        return True

    def render(self, display):
        self.camera_manager.render(display)
        self.hud.render(display)

    def destroy(self):
        actors = [
            self.camera_manager.sensor,
            self.collision_sensor.sensor,
            self.lane_invasion_sensor.sensor,
            self.vehicle]
        for actor in actors:
            if actor is not None:
                actor.destroy()


# ==============================================================================
# -- KeyboardControl -----------------------------------------------------------
# ==============================================================================


class KeyboardControl(object):
    def __init__(self, world, start_in_autopilot):
        start_in_autopilot = True
        self._autopilot_enabled = start_in_autopilot
        self._control = carla.VehicleControl()
        self._steer_cache = 0.0
        world.vehicle.set_autopilot(self._autopilot_enabled)
        world.hud.notification("Press 'H' or '?' for help.", seconds=4.0)

    def parse_events(self, world, clock):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return True
            elif event.type == pygame.KEYUP:
                if self._is_quit_shortcut(event.key):
                    return True
                elif event.key == K_BACKSPACE:
                    world.restart()
                elif event.key == K_F1:
                    world.hud.toggle_info()
                elif event.key == K_h or (event.key == K_SLASH and pygame.key.get_mods() & KMOD_SHIFT):
                    world.hud.help.toggle()
                elif event.key == K_TAB:
                    world.camera_manager.toggle_camera()
                elif event.key == K_c and pygame.key.get_mods() & KMOD_SHIFT:
                    world.next_weather(reverse=True)
                elif event.key == K_c:
                    world.next_weather()
                elif event.key == K_BACKQUOTE:
                    world.camera_manager.next_sensor()
                elif event.key > K_0 and event.key <= K_9:
                    world.camera_manager.set_sensor(event.key - 1 - K_0)
                elif event.key == K_r:
                    world.camera_manager.toggle_recording()
                elif event.key == K_q:
                    self._control.reverse = not self._control.reverse
                elif event.key == K_p:
                    self._autopilot_enabled = not self._autopilot_enabled
                    world.vehicle.set_autopilot(self._autopilot_enabled)
                    world.hud.notification('Autopilot %s' % ('On' if self._autopilot_enabled else 'Off'))
        if not self._autopilot_enabled:
            self._parse_keys(pygame.key.get_pressed(), clock.get_time())
            world.vehicle.apply_control(self._control)
        # record_dataset(world)

    def _parse_keys(self, keys, milliseconds):
        self._control.throttle = 1.0 if keys[K_UP] or keys[K_w] else 0.0
        steer_increment = 5e-4 * milliseconds
        if keys[K_LEFT] or keys[K_a]:
            self._steer_cache -= steer_increment
        elif keys[K_RIGHT] or keys[K_d]:
            self._steer_cache += steer_increment
        else:
            self._steer_cache = 0.0
        self._steer_cache = min(0.7, max(-0.7, self._steer_cache))
        self._control.steer = round(self._steer_cache, 1)
        self._control.brake = 1.0 if keys[K_DOWN] or keys[K_s] else 0.0
        self._control.hand_brake = keys[K_SPACE]

    @staticmethod
    def _is_quit_shortcut(key):
        return (key == K_ESCAPE) or (key == K_q and pygame.key.get_mods() & KMOD_CTRL)


# ==============================================================================
# -- Try to record the localization data and control output of autopilot -------
# ==============================================================================
def record_dataset(world):
    map = world.world.get_map()
    transform = world.vehicle.get_transform()
    location = transform.location
    waypoint = map.get_waypoint(transform.location)
    control = world.vehicle.get_control()
    write_in_csv(location, waypoint.transform, waypoint.lane_width, control)

def write_in_csv(location, waypoint_tf, lane_width, control, ds='localization_relative_coords_ds.csv'):
    # example of waypoint_tf Location(x=394.307587, y=-294.747772, z=0.000000) Rotation(pitch=360.000000, yaw=246.589417, roll=0.000000)
    #   location.z, rotation.pitch, rotation.roll may be helpful with ControlLoss scenario, where the chasis changes
    # example of vehicle control: VehicleControl(throttle=1.000000, steer=-0.001398, brake=0.000000, hand_brake=False, reverse=False, manual_gear_shift=False, gear=3)
    # TODO: check whether all control params are needed 
    
    # # ds1: 'localization_ds.csv'
    # row = [location.x, location.y, location.z, lane_width, waypoint_tf.location.x, waypoint_tf.location.y, waypoint_tf.location.z, \
    #     waypoint_tf.rotation.pitch, waypoint_tf.rotation.yaw, waypoint_tf.rotation.roll, control.throttle, control.steer]

    # ds2: 'localization_relative_coords_ds.csv'
    row = [location.x, location.y, location.z, lane_width, waypoint_tf.location.x-location.x, waypoint_tf.location.y-location.y, waypoint_tf.location.z-location.z, \
        waypoint_tf.rotation.pitch, waypoint_tf.rotation.yaw, waypoint_tf.rotation.roll, control.throttle, control.steer]

    # append the current data to csv file
    with open(ds, 'a+') as csvFile:
        writer = csv.writer(csvFile)
        writer.writerow(row)
        csvFile.close()

# ==============================================================================
# -- HUD -----------------------------------------------------------------------
# ==============================================================================

class HUD(object):
    def __init__(self, width, height):
        self.dim = (width, height)
        font = pygame.font.Font(pygame.font.get_default_font(), 20)
        fonts = [x for x in pygame.font.get_fonts() if 'mono' in x]
        default_font = 'ubuntumono'
        mono = default_font if default_font in fonts else fonts[0]
        mono = pygame.font.match_font(mono)
        self._font_mono = pygame.font.Font(mono, 14)
        self._notifications = FadingText(font, (width, 40), (0, height - 40))
        self.help = HelpText(pygame.font.Font(mono, 24), width, height)
        self.server_fps = 0
        self.frame_number = 0
        self.simulation_time = 0
        self._show_info = True
        self._info_text = []
        self._server_clock = pygame.time.Clock()

    def on_world_tick(self, timestamp):
        self._server_clock.tick()
        self.server_fps = self._server_clock.get_fps()
        self.frame_number = timestamp.frame
        self.simulation_time = timestamp.elapsed_seconds

    def tick(self, world, mapname, clock):
        if not self._show_info:
            return
        t = world.vehicle.get_transform()
        v = world.vehicle.get_velocity()
        c = world.vehicle.get_control()
        heading = 'N' if abs(t.rotation.yaw) < 89.5 else ''
        heading += 'S' if abs(t.rotation.yaw) > 90.5 else ''
        heading += 'E' if 179.5 > t.rotation.yaw > 0.5 else ''
        heading += 'W' if -0.5 > t.rotation.yaw > -179.5 else ''
        colhist = world.collision_sensor.get_collision_history()
        collision = [colhist[x + self.frame_number - 200] for x in range(0, 200)]
        max_col = max(1.0, max(collision))
        collision = [x / max_col for x in collision]
        vehicles = world.world.get_actors().filter('vehicle.*')
        self._info_text = [
            'Server:  % 16d FPS' % self.server_fps,
            'Client:  % 16d FPS' % clock.get_fps(),
            '',
            'Vehicle: % 20s' % get_actor_display_name(world.vehicle, truncate=20),
            'Map:     % 20s' % mapname,
            'Simulation time: % 12s' % datetime.timedelta(seconds=int(self.simulation_time)),
            '',
            'Speed:   % 15.0f km/h' % (3.6 * math.sqrt(v.x**2 + v.y**2 + v.z**2)),
            u'Heading:% 16.0f\N{DEGREE SIGN} % 2s' % (t.rotation.yaw, heading),
            'Location:% 20s' % ('(% 5.1f, % 5.1f)' % (t.location.x, t.location.y)),
            'Height:  % 18.0f m' % t.location.z,
            '',
            ('Throttle:', c.throttle, 0.0, 1.0),
            ('Steer:', c.steer, -1.0, 1.0),
            ('Brake:', c.brake, 0.0, 1.0),
            ('Reverse:', c.reverse),
            ('Hand brake:', c.hand_brake),
            '',
            'Collision:',
            collision,
            '',
            'Number of vehicles: % 8d' % len(vehicles)
        ]
        if len(vehicles) > 1:
            self._info_text += ['Nearby vehicles:']
            distance = lambda l: math.sqrt((l.x - t.location.x)**2 + (l.y - t.location.y)**2 + (l.z - t.location.z)**2)
            vehicles = [(distance(x.get_location()), x) for x in vehicles if x.id != world.vehicle.id]
            for d, vehicle in sorted(vehicles):
                if d > 200.0:
                    break
                vehicle_type = get_actor_display_name(vehicle, truncate=22)
                self._info_text.append('% 4dm %s' % (d, vehicle_type))
        self._notifications.tick(world, clock)

    def toggle_info(self):
        self._show_info = not self._show_info

    def notification(self, text, seconds=2.0):
        self._notifications.set_text(text, seconds=seconds)

    def error(self, text):
        self._notifications.set_text('Error: %s' % text, (255, 0, 0))

    def render(self, display):
        if self._show_info:
            info_surface = pygame.Surface((220, self.dim[1]))
            info_surface.set_alpha(100)
            display.blit(info_surface, (0, 0))
            v_offset = 4
            bar_h_offset = 100
            bar_width = 106
            for item in self._info_text:
                if v_offset + 18 > self.dim[1]:
                    break
                if isinstance(item, list):
                    if len(item) > 1:
                        points = [(x + 8, v_offset + 8 + (1.0 - y) * 30) for x, y in enumerate(item)]
                        pygame.draw.lines(display, (255, 136, 0), False, points, 2)
                    item = None
                    v_offset += 18
                elif isinstance(item, tuple):
                    if isinstance(item[1], bool):
                        rect = pygame.Rect((bar_h_offset, v_offset + 8), (6, 6))
                        pygame.draw.rect(display, (255, 255, 255), rect, 0 if item[1] else 1)
                    else:
                        rect_border = pygame.Rect((bar_h_offset, v_offset + 8), (bar_width, 6))
                        pygame.draw.rect(display, (255, 255, 255), rect_border, 1)
                        f = (item[1] - item[2]) / (item[3] - item[2])
                        if item[2] < 0.0:
                            rect = pygame.Rect((bar_h_offset + f * (bar_width - 6), v_offset + 8), (6, 6))
                        else:
                            rect = pygame.Rect((bar_h_offset, v_offset + 8), (f * bar_width, 6))
                        pygame.draw.rect(display, (255, 255, 255), rect)
                    item = item[0]
                if item: # At this point has to be a str.
                    surface = self._font_mono.render(item, True, (255, 255, 255))
                    display.blit(surface, (8, v_offset))
                v_offset += 18
        self._notifications.render(display)
        self.help.render(display)


# ==============================================================================
# -- FadingText ----------------------------------------------------------------
# ==============================================================================


class FadingText(object):
    def __init__(self, font, dim, pos):
        self.font = font
        self.dim = dim
        self.pos = pos
        self.seconds_left = 0
        self.surface = pygame.Surface(self.dim)

    def set_text(self, text, color=(255, 255, 255), seconds=2.0):
        text_texture = self.font.render(text, True, color)
        self.surface = pygame.Surface(self.dim)
        self.seconds_left = seconds
        self.surface.fill((0, 0, 0, 0))
        self.surface.blit(text_texture, (10, 11))

    def tick(self, _, clock):
        delta_seconds = 1e-3 * clock.get_time()
        self.seconds_left = max(0.0, self.seconds_left - delta_seconds)
        self.surface.set_alpha(500.0 * self.seconds_left)

    def render(self, display):
        display.blit(self.surface, self.pos)


# ==============================================================================
# -- HelpText ------------------------------------------------------------------
# ==============================================================================


class HelpText(object):
    def __init__(self, font, width, height):
        lines = __doc__.split('\n')
        self.font = font
        self.dim = (680, len(lines) * 22 + 12)
        self.pos = (0.5 * width - 0.5 * self.dim[0], 0.5 * height - 0.5 * self.dim[1])
        self.seconds_left = 0
        self.surface = pygame.Surface(self.dim)
        self.surface.fill((0, 0, 0, 0))
        for n, line in enumerate(lines):
            text_texture = self.font.render(line, True, (255, 255, 255))
            self.surface.blit(text_texture, (22, n * 22))
            self._render = False
        self.surface.set_alpha(220)

    def toggle(self):
        self._render = not self._render

    def render(self, display):
        if self._render:
            display.blit(self.surface, self.pos)


# ==============================================================================
# -- CollisionSensor -----------------------------------------------------------
# ==============================================================================


class CollisionSensor(object):
    def __init__(self, parent_actor, hud):
        self.sensor = None
        self._history = []
        self._parent = parent_actor
        self._hud = hud
        world = self._parent.get_world()
        bp = world.get_blueprint_library().find('sensor.other.collision')
        self.sensor = world.spawn_actor(bp, carla.Transform(), attach_to=self._parent)
        # We need to pass the lambda a weak reference to self to avoid circular
        # reference.
        weak_self = weakref.ref(self)
        self.sensor.listen(lambda event: CollisionSensor._on_collision(weak_self, event))

    def get_collision_history(self):
        history = collections.defaultdict(int)
        for frame, intensity in self._history:
            history[frame] += intensity
        return history

    @staticmethod
    def _on_collision(weak_self, event):
        self = weak_self()
        if not self:
            return
        actor_type = get_actor_display_name(event.other_actor)
        self._hud.notification('Collision with %r' % actor_type)
        impulse = event.normal_impulse
        intensity = math.sqrt(impulse.x**2 + impulse.y**2 + impulse.z**2)
        self._history.append((event.frame_number, intensity))
        if len(self._history) > 4000:
            self._history.pop(0)


# ==============================================================================
# -- LaneInvasionSensor --------------------------------------------------------
# ==============================================================================


class LaneInvasionSensor(object):
    def __init__(self, parent_actor, hud):
        self.sensor = None
        self._parent = parent_actor
        self._hud = hud
        world = self._parent.get_world()
        bp = world.get_blueprint_library().find('sensor.other.lane_invasion')
        self.sensor = world.spawn_actor(bp, carla.Transform(), attach_to=self._parent)
        # We need to pass the lambda a weak reference to self to avoid circular
        # reference.
        weak_self = weakref.ref(self)
        self.sensor.listen(lambda event: LaneInvasionSensor._on_invasion(weak_self, event))

    @staticmethod
    def _on_invasion(weak_self, event):
        self = weak_self()
        if not self:
            return
        text = ['%r' % str(x).split()[-1] for x in set(event.crossed_lane_markings)]
        self._hud.notification('Crossed line %s' % ' and '.join(text))


# ==============================================================================
# -- CameraManager -------------------------------------------------------------
# ==============================================================================


class CameraManager(object):
    def __init__(self, parent_actor, hud):
        self.sensor = None
        self._surface = None
        self._parent = parent_actor
        self._hud = hud
        self._recording = False
        self._camera_transforms = [
            carla.Transform(carla.Location(x=2.0, y=0.0, z=1.4)),  # x=1.6, z=1.7
            carla.Transform(carla.Location(x=-5.5, z=2.8), carla.Rotation(pitch=-15))]
        self._transform_index = 0 # originally is 1, change default camera to the front one
        self._sensors = [
            ['sensor.camera.rgb', cc.Raw, 'Camera RGB'],
            ['sensor.camera.depth', cc.Raw, 'Camera Depth (Raw)'],
            ['sensor.camera.depth', cc.Depth, 'Camera Depth (Gray Scale)'],
            ['sensor.camera.depth', cc.LogarithmicDepth, 'Camera Depth (Logarithmic Gray Scale)'],
            ['sensor.camera.semantic_segmentation', cc.Raw, 'Camera Semantic Segmentation (Raw)'],
            ['sensor.camera.semantic_segmentation', cc.CityScapesPalette, 'Camera Semantic Segmentation (CityScapes Palette)'],
            ['sensor.lidar.ray_cast', None, 'Lidar (Ray-Cast)']]
        world = self._parent.get_world()
        bp_library = world.get_blueprint_library()
        for item in self._sensors:
            bp = bp_library.find(item[0])
            if item[0].startswith('sensor.camera'):
                bp.set_attribute('image_size_x', str(hud.dim[0]))
                bp.set_attribute('image_size_y', str(hud.dim[1]))
            item.append(bp)
        self._index = None

        self.num_wps = None

    def toggle_camera(self):
        self._transform_index = (self._transform_index + 1) % len(self._camera_transforms)
        self.sensor.set_transform(self._camera_transforms[self._transform_index])

    def set_sensor(self, index, notify=True):
        index = index % len(self._sensors)
        needs_respawn = True if self._index is None \
            else self._sensors[index][0] != self._sensors[self._index][0]
        if needs_respawn:
            if self.sensor is not None:
                self.sensor.destroy()
                self._surface = None
            self.sensor = self._parent.get_world().spawn_actor(
                self._sensors[index][-1],
                self._camera_transforms[self._transform_index],
                attach_to=self._parent)
            # We need to pass the lambda a weak reference to self to avoid
            # circular reference.
            weak_self = weakref.ref(self)
            # set the callback method and modify it to collect dataset
            self.sensor.listen(lambda image: CameraManager._parse_image(weak_self, image))       
            # self.sensor.listen(lambda image: CameraManager._parse_image_and_save(self, image))
        if notify:
            self._hud.notification(self._sensors[index][2])
        self._index = index

    def next_sensor(self):
        self.set_sensor(self._index + 1)

    def toggle_recording(self):
        self._recording = not self._recording
        self._hud.notification('Recording %s' % ('On' if self._recording else 'Off'))

    def render(self, display):
        if self._surface is not None:
            display.blit(self._surface, (0, 0))

    @staticmethod
    def _parse_image(weak_self, image, save_dir=None):
        self = weak_self()
        if not self:
            return
        if self._sensors[self._index][0].startswith('sensor.lidar'):
            points = np.frombuffer(image.raw_data, dtype=np.dtype('f4'))
            points = np.reshape(points, (int(points.shape[0]/3), 3))
            lidar_data = np.array(points[:, :2])
            lidar_data *= min(self._hud.dim) / 100.0
            lidar_data += (0.5 * self._hud.dim[0], 0.5 * self._hud.dim[1])
            lidar_data = np.fabs(lidar_data)
            lidar_data = lidar_data.astype(np.int32)
            lidar_data = np.reshape(lidar_data, (-1, 2))
            lidar_img_size = (self._hud.dim[0], self._hud.dim[1], 3)
            lidar_img = np.zeros(lidar_img_size)
            lidar_img[tuple(lidar_data.T)] = (255, 255, 255)
            self._surface = pygame.surfarray.make_surface(lidar_img)
        else:
            image.convert(self._sensors[self._index][1])
            array = np.frombuffer(image.raw_data, dtype=np.dtype("uint8"))
            array = np.reshape(array, (image.height, image.width, 4))
            array = array[:, :, :3]
            array = array[:, :, ::-1]
            self._surface = pygame.surfarray.make_surface(array.swapaxes(0, 1))
        if self._recording:
            # RH: change the path from '_out/%08d' to 'data/%08d', 
            #   seems to restore all prev files even after "moving the folder to Trash"
            # image.save_to_disk('data_ctv/%08d' % image.frame_number)
            if save_dir is None:
                raise ValueError("no valid dir to save image")
            image.save_to_disk(save_dir+'{:08d}'.format(image.frame_number))

    def save_control_for_e2c(self, frame_number, control, save_dir):
        path = save_dir+'{:08d}_ctv'.format(frame_number) # keep consistent with image.save_to_disk
        print("path for npy", path)
        x = np.array([control.throttle, control.steer, control.brake])
        print("control in array", x)
        np.save(path, x)


    def save_ctv_for_e2c(self, frame_number, control, transform, velocity, save_dir):
        path = save_dir+'{:08d}_ctv'.format(frame_number) # keep consistent with image.save_to_disk
        loc = transform.location
        rot = transform.rotation
        x = np.array([control.throttle, control.steer, control.brake, \
                      loc.x, loc.y, loc.z, rot.yaw, rot.pitch, rot.roll, \
                      velocity.x, velocity.y, velocity.z])
        # print("ctv in array", x)
        np.save(path, x)

    def save_normalized_ctv(self, frame_number, control, loc_diff, velocity, save_dir, max_loc_diff = 30, max_vel = 90.0):
        path = save_dir+'{:08d}_ctv'.format(frame_number) # keep consistent with image.save_to_disk
        
        x = np.array([control.throttle, control.steer, control.brake, \
                      norm(loc_diff.x, max_loc_diff), norm(loc_diff.y, max_loc_diff), norm(loc_diff.z, max_loc_diff), \
                      norm(velocity.x, max_vel), norm(velocity.y, max_vel), norm(velocity.z, max_vel)])

        # print("nomalized ctv", x)
        np.save(path, x)

    def save_normalized_ctv_wps(self, frame_number, control, loc_diffs, velocity, save_dir, max_loc_diff = 60, max_vel = 90.0):
        path = save_dir+'{:08d}_ctv'.format(frame_number)
        x = np.array([control.throttle, control.steer, control.brake])
        for loc_diff in loc_diffs:
            # wp here is already relative
            x = np.hstack((x, np.array([norm(loc_diff.x, max_loc_diff), norm(loc_diff.y, max_loc_diff), norm(loc_diff.z, max_loc_diff)])))

        x = np.hstack((x, np.array([norm(velocity.x, max_vel), norm(velocity.y, max_vel), norm(velocity.z, max_vel)])))
        # print("save normalized_ctv_wps", x.shape)
        np.save(path, x)

    def _parse_image_and_save(self, image):
        # parse the image as above
        # Note: convert, save_to_disk methods are from carla.Image class 
        # see https://carla.readthedocs.io/en/latest/python_api/#carlaimagecarlasensordata-class
        self.num_wps = 50
        save_dir = 'data_ctv_logdepth_norm_catwp_{}/'.format(self.num_wps)
        sampling_radius = 90.0*1/3.6 # max_dist that the vehicle can reach in the next second
        weak_self = weakref.ref(self)

        # self._parse_image(weak_self, image, save_dir)

        # save control in another file with same frame number so that it's easier to read data
        frame_number = image.frame_number
        control = self._parent.get_control() # CameraManager._parent => world.vehicle
        transform = self._parent.get_transform()
        # TODO save yaw angle
        rot = transform.rotation
        velocity = self._parent.get_velocity()

        map = self._parent.get_world().get_map()
        # use single waypoint
        # waypoint = random.choice(map.get_waypoint(transform.location).next(sampling_radius))
        # concatenate a series of waypoints
        sub_sampling_radius = 1.0
        w0 = map.get_waypoint(transform.location) 
        wps = []
        wps.append(w0)
        for i in range(1, self.num_wps):
            wps.append(wps[i-1].next(sub_sampling_radius)[0])

        # print("concatenate waypoints", len(wps))
        loc_diffs = []
        for wp in wps:
            loc_diffs.append(transform.location - wp.transform.location)
        
        # rot_diff = transform.rotation - wp_tf.rotation # TypeError: unsupported operand type(s) for -: 'Rotation' and 'Rotation'

        # self.save_control_for_e2c(frame_number, control, save_dir)
        # self.save_ctv_for_e2c(frame_number, control, transform, velocity, save_dir)
        # self.save_normalized_ctv(frame_number, control, loc_diff, velocity, save_dir)

        # self.save_normalized_ctv_wps(frame_number, control, loc_diffs, velocity, save_dir)
        

def norm(x, x_max):
    n = x/(x_max*2) + 0.5
    if n>0 and n< 1:
        return n
    else:
        raise ValueError("abnormal norm x {}, x_max {}, norm {}".format(x, x_max, n))


# ==============================================================================
# -- game_loop() ---------------------------------------------------------------
# ==============================================================================


def game_loop(args):
    pygame.init()
    pygame.font.init()
    world = None
    outfile = "long_states_2.csv"

    try:
        client = carla.Client(args.host, args.port) #  worker_threads=1
        client.set_timeout(2.0)

        display = pygame.display.set_mode(
            (args.width, args.height),
            pygame.HWSURFACE | pygame.DOUBLEBUF)

        hud = HUD(args.width, args.height) # display, clock, and text info
        world = World(client.get_world(), hud)
        controller = KeyboardControl(world, args.autopilot)

        clock = pygame.time.Clock()
        # modify the game loop to collect data with different init conditions

        # 1. get the "origin" waypoint
        num_wp = 500
        num_init_pt = 200
        horizon = 50
        start_loc = carla.Location(x=405.405, y=-59.49, z=0.0)      
    
        map = world.world.get_map()
        org_wp = map.get_waypoint(start_loc) # project to the road
        org_loc = org_wp.transform.location
        sampling_radius = 2.0
        wps = [org_wp]
        for i in range(num_wp):
            # sample ref waypoint per sampling_radius
            wp = random.choice(wps[i].next(sampling_radius))
            print("wp {}".format(i), wp)
            wps.append(wp)

            # generate a list of random starting point around the ref wps
            x0s = generate_random_starting_pts(wp, num=num_init_pt) # pass wp or wps[i], waste either the first or the last pt
            
            for x0 in x0s:
                #x0 is transform object
                # RH: try to set initial velocity for autopilot
                
                # t_0 set 
                world.vehicle.set_transform(x0)
                init_vel = generate_random_velocity(max_vel = 30)
                world.vehicle.set_velocity(init_vel)
                # print("set init state", hud.frame_number, hud.simulation_time)
                print(x0, init_vel)
                world.world.wait_for_tick()
                init_frame_number = hud.frame_number
                

                while hud.frame_number-init_frame_number<400: # 20s
                    # tick             
                    if controller.parse_events(world, clock): # e.g. press `Esc` key
                        print("Escape")
                        return
                    if not world.tick(clock): # e.g. no cars
                        print("No ego vehicle")
                        return
                    # world.render(display)
                    # pygame.display.flip()
                    # clock.tick_busy_loop(60) # move to the bottom TODO: check whether it works
                    # # world.world.wait_for_tick(1)
                                    
                    # t_1 get current state
                    world.world.wait_for_tick()
                    cur_loc = world.vehicle.get_transform()
                    cur_vel = world.vehicle.get_velocity()
                    control = world.vehicle.get_control()
                    # print("get current state", hud.frame_number, hud.simulation_time)
                    print(cur_loc, cur_vel)

                    # concatenate future_wps within horizon
                    cur_wp = map.get_waypoint(cur_loc.location)
                    future_wps = []
                    future_wps.append(cur_wp)

                    for j in range(horizon):
                        future_wps.append(random.choice(future_wps[-1].next(sampling_radius)))

                    future_wps_np = []
                    for future_wp in future_wps:
                        future_wps_np.append(np.array([future_wp.transform.location.x, future_wp.transform.location.y]))
                    future_wps_np = np.array(future_wps_np)
                    future_wps_np = future_wps_np - np.array([cur_wp.transform.location.x, cur_wp.transform.location.y])
                    # print("future_wps_np", future_wps_np.shape, future_wps_np.flatten().shape) #future_wps_np (51, 2) (102,)


                    # print(control)                    

                    # tick 
                    world.world .wait_for_tick()                 
                    
                    # t_2 get next state
                    next_loc = world.vehicle.get_transform()
                    next_vel = world.vehicle.get_velocity()

                    # TODO: save the states and transition
                    # df = pd.DataFrame({'cur_loc': transform_to_arr(cur_loc), \
                    #                    'cur_vel': np.array([cur_vel.x, cur_vel.y, cur_vel.z]), \
                    #                    'next_loc': transform_to_arr(next_loc),\
                    #                    'next_vel': np.array([next_vel.x, next_vel.y, next_vel.z]), \
                    #                    'cur_loc_rl': np.array([cur_loc.location.x, cur_loc.location.y])- np.array([cur_wp.transform.location.x, cur_wp.transform.location.y]), \
                    #                    'future_wps': future_wps_np.flatten(), \
                    #                    'next_loc_rl': np.array([next_loc.location.x, next_loc.location.y])- np.array([cur_wp.transform.location.x, cur_wp.transform.location.y])})
                    # with open(outfile, 'a') as f:
                    #     df.to_csv(f)
                    row = list(np.hstack((np.array([control.throttle, control.steer, control.brake]), \
                                          transform_to_arr(cur_loc), np.array([cur_vel.x, cur_vel.y, cur_vel.z]),\
                                          transform_to_arr(next_loc), np.array([next_vel.x, next_vel.y, next_vel.z]), \
                                          np.array([cur_loc.location.x, cur_loc.location.y])- np.array([cur_wp.transform.location.x, cur_wp.transform.location.y]), \
                                          future_wps_np.flatten(), \
                                          np.array([next_loc.location.x, next_loc.location.y])- np.array([cur_wp.transform.location.x, cur_wp.transform.location.y]))))

                    with open(outfile, 'a+') as csvFile:
                        writer = csv.writer(csvFile)
                        writer.writerow(row)
                        csvFile.close()

                    world.render(display)
                    pygame.display.flip()
                    clock.tick_busy_loop(60)



    finally:
        if world is not None:
            world.destroy()

        pygame.quit()


def transform_to_arr(tf):
    return np.array([tf.location.x, tf.location.y, tf.location.z, tf.rotation.pitch, tf.rotation.yaw, tf.rotation.roll])

# ==============================================================================
# --methods used for modified game loop -------
# ==============================================================================
def generate_random_starting_pts(wp, num=100):
    """
    generate a list of random starting point around one wp
    params: 
        wp: carla.Waypoint object
        num: number of starting ponts to be generated

    """
    center = wp.transform.location
    radius = 1.8
    if radius < wp.lane_width/2.0:
        raise ValueError("radius < wp.lane_width/2")
    x0s = []
    for i in range(num):
        # get random location
        x = center.x + radius*(2*random.random()-1.0)
        y = center.y + radius*(2*random.random()-1.0)
        z = center.z # keep the same height
        # get random rotation
        yaw = 180*(2*random.random()-1.0) # rotation.yaw is stored in degrees
        x0s.append(carla.Transform(location=carla.Location(x=x, y=y, z=z), rotation=carla.Rotation(yaw=yaw)))
    # print("one of {} samples starting pts around {}: {}".format(len(x0s), wp, x0s[0]))
    return x0s

def generate_random_velocity(max_vel=30):
    return carla.Vector3D(max_vel*(2*random.random()-1.0), max_vel*(2*random.random()-1.0), 0)


# ==============================================================================
# -- main() --------------------------------------------------------------------
# ==============================================================================


def main():
    argparser = argparse.ArgumentParser(
        description='CARLA Manual Control Client')
    argparser.add_argument(
        '-v', '--verbose',
        action='store_true',
        dest='debug',
        help='print debug information')
    argparser.add_argument(
        '--host',
        metavar='H',
        default='127.0.0.1',
        help='IP of the host server (default: 127.0.0.1)')
    argparser.add_argument(
        '-p', '--port',
        metavar='P',
        default=2000,
        type=int,
        help='TCP port to listen to (default: 2000)')
    argparser.add_argument(
        '-a', '--autopilot',
        action='store_true',
        help='enable autopilot')
    argparser.add_argument(
        '--res',
        metavar='WIDTHxHEIGHT',
        default='200x88',
        help='window resolution (default: 200x88, originally default: 1280x720)')
    args = argparser.parse_args()

    # RH: mmodify the windows_size to collect smaller images
    args.width, args.height = [int(x) for x in args.res.split('x')]

    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(format='%(levelname)s: %(message)s', level=log_level)

    logging.info('listening to server %s:%s', args.host, args.port)

    print(__doc__)

    try:

        game_loop(args)

    except KeyboardInterrupt:
        print('\nCancelled by user. Bye!')
    except Exception as error:
        logging.exception(error)


if __name__ == '__main__':

    main()