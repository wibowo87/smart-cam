#!/usr/bin/python

import time
import picamera
import picamera.array
import cv2
import multiprocessing
import os
import boto
#import pprint
from motion_utils.misc import upload_to_s3
from motion_utils.db import DynamoDBUtils
import math
from scipy import ndimage
import numpy as np
from scipy.spatial import distance
from flask import Flask, Response

RPiName = 'FrontDoor'
FRAMES_PER_CLIP = 100    # This is FPS times VIDEO_LENGTH
FPS = 10
VIDEO_LENGTH = 10        # Is seconds
BUCKET_NAME = 'smart-cam'

def getMotionFromFrame(frame, threshold=0.05):
    # This function returns True if mass of truth is greater than threshold
    frameCopy = frame.copy()
    totalMass = frameCopy.size

    frameCopy[frameCopy > 0] = 1        # Converting all non-zero pixels to 1
    frameCopyMass = frameCopy.sum()
    fraction = frameCopyMass/float(totalMass)
    
    if fraction > threshold:
        response = 1    # True
    else:
        response = 0    # False

    return response, round(fraction, 4)

def writeToFrame(frameTimestamp, frame, RPiName):
    # Writing SmartCam
    font = cv2.FONT_HERSHEY_SIMPLEX
    caption = 'SmartCam: ' +  RPiName
    cv2.putText(frame, 'MIDS Capstone Project', (30,30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1, 8)
    cv2.putText(frame, caption, (30,50), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1, 8)
    cv2.putText(frame, frameTimestamp, (30,70), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1, 8)
    return frame

def cameraReader(cam_writer_frames_Queue, cam_liveWeb_frame_Queue):
    
    camera = picamera.PiCamera()
    camera.resolution = (320, 240)
    camera.framerate = FPS
    stream = picamera.array.PiRGBArray(camera)

    while True:

        FRAMES = list()
        t1 = time.time()
        startTime = time.time()
        for c in xrange(FRAMES_PER_CLIP):
            frameTimestamp = time.asctime() + ' ' + time.tzname[0]
            camera.capture(stream, format='bgr', use_video_port=True)
            frame = stream.array
            if cam_liveWeb_frame_Queue.full() == False:
                cam_liveWeb_frame_Queue.put(frame, block=False)
            FRAMES.append((frameTimestamp, frame))
            stream.truncate(0)
        print "Camera Capture", time.time() - t1
        
        # Sending frame to processing process
        cam_writer_frames_Queue.put((startTime, FRAMES))
        del FRAMES

    return
    camera.close()

def videoWriter(cam_writer_frames_Queue, writer_blurrer_filename_Queue):
    while True:
        startTime, FRAMES = cam_writer_frames_Queue.get()
        t1 = time.time()
        # Writing frames to disk
        #fourcc = cv2.cv.CV_FOURCC(*'XVID')
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        fourcc_out = cv2.VideoWriter_fourcc(*'avc1')
        filename_blurs = 'blurrer' + '_' + RPiName + '_' + repr(startTime) + ".avi"
        clipWriter = cv2.VideoWriter(filename_blurs, fourcc, 10, (320, 240))

        for frameTimestamp, frame in FRAMES:
            clipWriter.write(frame)

        writer_blurrer_filename_Queue.put(filename_blurs)

        filename = RPiName + '_' + repr(startTime) + ".mp4"
        clipWriter = cv2.VideoWriter(filename, fourcc_out, 10, (320, 240))

        
        while len(FRAMES) > 0:
            frameTimestamp, frame = FRAMES.pop(0)
            frameWithCaption = writeToFrame(frameTimestamp, frame, RPiName)
            clipWriter.write(frameWithCaption)
        print "Written to disk: ", time.time() - t1

def frameBlurrer(writer_blurrer_filename_Queue, blur_to_motiondetector_blurred_Queue):
    while True:
        BLURS = list()
        FRAMES = list()
        filename = writer_blurrer_filename_Queue.get()
        
        t1 = time.time()
        camera = cv2.VideoCapture(filename)
        for counter in xrange(0, FRAMES_PER_CLIP):
            ret, frame = camera.read()
            FRAMES.append(frame)
        camera.release()

        while len(FRAMES) > 0:
            frame = FRAMES.pop(0)
            blurred = cv2.medianBlur(frame, 9)
            BLURS.append(blurred)
        
        print "Blurred", time.time() - t1

        # Sending blurs to motion detector
        blur_to_motiondetector_blurred_Queue.put((filename, BLURS))
        del filename
        del BLURS
    return

def getCenterOfMass(frame):
    center = ndimage.measurements.center_of_mass(frame)
    if math.isnan(center[0]) == True:
        # Setting center to absolute middle of frame
        c = [i/2 for i in frame.shape]
    else:
        c = [int(x) for x in center]   # Converting center of mass into integer
    c.reverse()
    return c

def getMeanCenters(lastMeanCenter, CENTERS):
    MEANCENTERS = list()
    MEANCENTERS.append(lastMeanCenter)
    for i in xrange(FPS):
        chunk = np.array(CENTERS[i*FPS:(i+1)*FPS])
        meanCenter = list(np.mean(chunk, axis=0).astype(int))
        MEANCENTERS.append(meanCenter)
    return MEANCENTERS            

def getMotionFromCenters(meanCenters):
    DISTANCES = list()
    for c in xrange(FPS):
        dst = distance.euclidean(meanCenters[c], meanCenters[c+1])
        DISTANCES.append(dst)
    if meanCenters[0] == [-1, -1]:
        DISTANCES.pop(0)
    return DISTANCES

def motionDetecter(blur_to_motiondetector_blurred_Queue, file_Queue):
    # Creating MOG object
    #fgbg = cv2.BackgroundSubtractorMOG()
    fgbg = cv2.createBackgroundSubtractorMOG2()

    lastMeanCenter = [-1, -1]

    # Start infinite loop here
    while True:
        motionFlag = 0
        FRACTIONS = list()
        FOREGROUND = list()
        CENTERS = list()
        
        # Receiving FRAMES
        filename, BLURS = blur_to_motiondetector_blurred_Queue.get()

        t1 = time.time()
        while len(BLURS) > 0:
            blurred = BLURS.pop(0)
            edges = cv2.Canny(blurred, 160, 200)
            CENTERS.append(getCenterOfMass(edges))
            fgmask = fgbg.apply(blurred)
            ret, frac = getMotionFromFrame(fgmask)
            motionFlag += ret
            FRACTIONS.append(frac)
        
        del BLURS
        # Getting max foreground percent for every 10 frames
        for i in xrange(VIDEO_LENGTH):
            FOREGROUND.append(max(FRACTIONS[FPS*i:FPS*(i+1)]))

        meanCenters = getMeanCenters(lastMeanCenter, CENTERS)
        lastMeanCenter = meanCenters[-1]
        motionList = getMotionFromCenters(meanCenters)

        # Writing output to file
        # remove the 'blurrer_' from the filename
        with open(filename[8:-4]+'.motion', 'w') as f:
            f.write(str(motionFlag) + '\n')
            f.write(str(FOREGROUND))
            f.write(str(motionList) + '\n')
            f.close()

        # Deleteing temporary used by Blurrer
        os.remove(filename)
        print "Processed MOG and Center of Mass", time.time() - t1

        # upload video and metadata to AWS if motion detected
        if motionFlag > 0 and max(motionList) > 5:
            file_Queue.put((filename, FOREGROUND))
        # delete the video and motion files otherwise
        else:
            os.remove(filename[8:-4]+'.mp4')
            os.remove(filename[8:-4]+'.motion')
                
    return

# process for uploading data to S3 and Dynamo
def uploader(file_Queue, db):
    while True:
        filename, foreground = file_Queue.get()
        # make sure filename extension is mp4
        filename = filename[:-3] + 'mp4'

        upload_to_s3(BUCKET_NAME, "videos/"+filename[8:], filename[8:])
        #print filename + ", " +  filename[8+len(RPiName)+1:-4]
        # extract the timestamp from the filename
        timestamp = float(filename[8+len(RPiName)+1:-4])
        # convert float array to strings as needed for Dynamo
        fg_data = {'data': [str(item) for item in foreground]}
        db.create_item(RPiName, BUCKET_NAME, 'videos/'+filename[8:], timestamp, fg_data)
        # delete the files once we've uploaded them
        os.remove(filename[8:])
        os.remove(filename[8:-3]+"motion")
    return

def writeToLiveFrame(frame, RPiName):
    # Writing SmartCam
    font = cv2.FONT_HERSHEY_SIMPLEX
    caption = 'SmartCam: ' +  RPiName + ' (LIVE Feed)'
    frameTimestamp = time.asctime() + ' ' + time.tzname[0]
    cv2.putText(frame, 'MIDS Capstone Project', (30,30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1, 8)
    cv2.putText(frame, caption, (30,50), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1, 8)
    cv2.putText(frame, frameTimestamp, (30,70), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1, 8)
    return frame

def liveVideoServer(cam_liveWeb_frame_Queue):
    app = Flask(__name__)

    def gen():
        while True:
            frame = cam_liveWeb_frame_Queue.get()
            frame = writeToLiveFrame(frame, RPiName)
            image = cv2.imencode('.jpg', frame)[1].tostring()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + image + b'\r\n')

    @app.route("/liveVideoFeed")
    def liveFeed():
        return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame')

    app.run(host='0.0.0.0')

if __name__ == "__main__":    
    cam_writer_frames_Queue = multiprocessing.Queue()
    writer_blurrer_filename_Queue = multiprocessing.Queue()
    blur_to_motiondetector_blurred_Queue = multiprocessing.Queue()
    file_Queue = multiprocessing.Queue()
    db = DynamoDBUtils()
    cam_liveWeb_frame_Queue = multiprocessing.Queue(maxsize=1)

    camReader1_t = multiprocessing.Process(target=cameraReader, args=(cam_writer_frames_Queue, cam_liveWeb_frame_Queue,))
    videoWriter_t = multiprocessing.Process(target=videoWriter, args=(cam_writer_frames_Queue,writer_blurrer_filename_Queue,))
    frameBlurrer_t = multiprocessing.Process(target=frameBlurrer, args=(writer_blurrer_filename_Queue, blur_to_motiondetector_blurred_Queue))
    motionDetector_t = multiprocessing.Process(target=motionDetecter, args=(blur_to_motiondetector_blurred_Queue, file_Queue))
    uploader_t = multiprocessing.Process(target=uploader, args=(file_Queue, db))
    liveFeed_t = multiprocessing.Process(target=liveVideoServer, args=(cam_liveWeb_frame_Queue,))

    camReader1_t.start()
    videoWriter_t.start()
    frameBlurrer_t.start()
    motionDetector_t.start()
    uploader_t.start()
    liveFeed_t.start()
