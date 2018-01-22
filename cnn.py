#! /usr/bin/env python3

#Visualise with: 'tensorboard --logdir='logs''

#Imports
import sys                          #For embedded python workaround
import os                           #For file reading and warning suppression

#Embedded python work around
if not hasattr(sys, 'argv'):
    sys.argv = ['']

import shutil                       #For directory deletion
import numpy as np                  #For transforming blocks
import matplotlib.pyplot as plt     #For visualisation
from texttable import Texttable     #For outputting
import math                         #For logs
import time                         #For debugging with catchup
import subprocess                   #For compiling graphs
import socket                       #For IPC
import select                       #For waiting for socket to fill
import errno                        #Handling socket errors
import struct                       #For converting kdu_uint32s to uint32s
import tensorflow as tf             #For deep learning
from mvnc import mvncapi as mvnc    #For Movidius NCS API

#Set suppressed logging level for Movidius NCS API
mvnc.SetGlobalOption(mvnc.GlobalOption.LOG_LEVEL, 1)

#Set non-verbose error checking for tensorflow
tf.logging.set_verbosity(tf.logging.ERROR)
os.environ['TF_CPP_MIN_LOG_LEVEL']='1'  #1 filters all
                                        #2 filters warnings
                                        #3 filters none

#Global variables
#Relative paths
GRAPHS_FOLDER = "./graphs"
LOGS_FOLDER = "./logs"

#Names of inputs and outputs in tensorflow graph (for compiling for NCS)
#See 'new_graph' function for options
INPUT_NAME = "images"
OUTPUT_NAME = "predictor"

#Hardcoded image input dimensions
WIDTH = 32
HEIGHT = 32

#Globals for creating graphs
FILTER_SIZES    =   [5, 5]      #Convolutional layer filter sizes in pixels
NUM_FILTERS     =   [16, 32]     #Number of filters in each Convolutional layer
FC_SIZES        =   [384, 64]   #Number of neurons in fully connected layer

#Converts to frequency domain and applies a frequency cutoff on a numpy array
#representing an image. Cutoff: <1 for low freq, >200 for high freq
def apply_frequency_cutoff(img_matrix, cutoff):
    #Convert to freq domain
    img_matrix =  np.fft.fft2(img_matrix)
    img_matrix *= 255.0 / img_matrix.max() #Scale to [0, 255]

    #Apply cutoff to real part of freq (<1 for low freq, >200 for high freq)
    cutoff = np.abs(img_matrix) < cutoff
    img_matrix[cutoff] = 0

    #Back to spacial domain
    img_matrix =  np.fft.ifft2(img_matrix)
    img_matrix =  np.abs(img_matrix)        #Complex->real
    img_matrix =  np.log10(img_matrix)      #Take log scale
    img_matrix *= 255.0 / img_matrix.max()  #Scale to [0, 255]
    #img_matrix = 255 - img_matrix           #Invert

    return img_matrix

#Uniquely saves a figure of the imgage represented by the supplied array
#in the output folder
def save_array_as_fig(img_array, name):
    #Create graph to ensure that block was read correctly
    fig = plt.figure(name, figsize=(15, 15), dpi=80)  #dims*dpi = res

    #Constrain axis proportions and plot
    plt.gca().set_aspect('equal', adjustable='box')
    plt.imshow( img_array, cmap="Greys_r", vmin=0, vmax=255,
                interpolation='nearest')

    global FILE_SUF

    fig.savefig("output/" + name)

    #Explicity close figure for memory usage
    plt.close(fig)

#Transforms the standard uint8 1 channel image gotten from the Kakadu SDK into
#a three channel RGB (redundant channels) half precision float (float16) image
#such that it is compatible with the Movidius NCS architecture
def make_compatible(image_data, save_image):
    #Reshape to placeholder dimensions
    output = np.reshape(image_data, (WIDTH, HEIGHT))

    #Cast to 8-bit unsigned integer
    output = np.uint8(output)

    #Output an image if required while uint8
    if save_image:
        save_array_as_fig(output, 'test')

    #Now cast
    output = np.float16(output)

    #Add two more channels to get RBG
    output = np.dstack([output]*3)

    #Give it back
    return output

#Converts socket recieved bytes to integers
def byte_string_to_int_array(bytes):
    data = []
    i = 0
    while i < len(bytes):
        data.append(struct.unpack('I', bytes[i:(i + 4)]))
        i = i + 4
    return data

#Boots up a concurrently running client that keeps the trainable graph in memory
#to speed up training time. Updates depending on argument at the end of its run
def run_training_client(graph_name, port, optimise_and_save, batch_size, total_units):
    #Connect to specified port
    print("Connecting client to port " + str(port))
    sock = socket.socket()
    sock.connect(('', port))
    sock.setblocking(0) #Throw an exception when out of data to read (non-blocking)
    timeout = 3         #Three second timeout

    #Make sure graph structure is reset before opening session
    tf.reset_default_graph()

    #Begin a tensorflow session
    sess = tf.Session()

    #Load the graph to be trained & keep the saver for later updating
    print("Loading graph '" + graph_name + "' for " + ("training" if optimise_and_save == 1 else "validation"))
    saver = restore_model(graph_name, sess)

    #Prepare to track information
    t_pos = 0   #Correct classified a galaxy
    f_pos = 0   #Guessed galaxy when should have been noise
    t_neg = 0   #Correctly classified noise
    f_neg = 0   #Guessed noise when should have been galaxy

    #Recieve supervised data. Take 4 times as many as recieving uint32s (kdu_uint32s)
    #and convert
    recieving = True
    image_batch = []
    label_batch = []
    batch_num = 0
    units_num = 0
    while recieving:
        #When ready then recv
        ready = select.select([sock], [], [], timeout)
        if ready[0]:
            #If WIDTH*HEIGHT*4 units are recieved then this is an image
            image_bytes = sock.recv((WIDTH*HEIGHT)*4)
            image_input = byte_string_to_int_array(image_bytes)
            if len(image_input) == WIDTH*HEIGHT:
                #Make the image tensorflow graph compatible
                image_batch.append(make_compatible(image_input, False))
            else:
                #No data found in socket, stop recving
                print("Error: image data not recv'd correctly, finishing early")
                break
        else:
            if len(image_batch) != 0:
                print("No data found in socket, remaining batch had " + str(len(image_batch)) + " units:")
            recieving = False

        #When ready then recv
        if recieving:   #Check if still reciving (image could've finished)
            ready = select.select([sock], [], [], timeout)
            if ready[0]:
                #If 4 units are recieved then this is a label
                label_bytes = sock.recv(1*4)
                label_input = byte_string_to_int_array(label_bytes)
                if len(label_input) == 1:
                    #Make the label tensorflow compatible
                    label_batch.append(label_input[0])
                else:
                    #No data found in socket, stop recving
                    print("Error: label data not recv'd correctly, finishing early")
                    break
            else:
                print("No label data found in socket, ending")
                recieving = False

        #Increment if a label and image came trhough (one unit)
        if recieving:
            units_num = units_num + 1

        #If at end of data feed or if the batch is full then feed to graph
        batch_ready = len(image_batch) == batch_size and len(label_batch) == batch_size;
        if batch_ready or (not recieving and len(image_batch) != 0):
            #Turn recieved info into a feeder dictionary
            feed_dict = {   'images:0': image_batch,
                            'labels:0': label_batch,
                            'is_training:0': (optimise_and_save == 1)}

            #If training then train
            if optimise_and_save == 1:
                #Feed into the network so it can 'learn' by running the adam optimiser
                sess.run('Adam', feed_dict=feed_dict)

            #What is the prediction for each image? (as prob [0,1])
            preds = sess.run('predictor:0', feed_dict=feed_dict)

            #Redundant for clarity here
            batch_t_pos, batch_t_neg, batch_f_pos, batch_f_neg = 0, 0, 0, 0
            for i in range(len(image_batch)):
                pred_gal = preds[i][0] > 0.5
                is_gal = label_batch[i][0] == 1

                #Count types of failure
                if pred_gal and is_gal:
                    batch_t_pos = batch_t_pos + 1
                elif pred_gal and not is_gal:
                    batch_f_pos = batch_f_pos + 1
                elif not pred_gal and is_gal:
                    batch_f_neg = batch_f_neg + 1
                elif not pred_gal and not is_gal:
                    batch_t_neg = batch_t_neg + 1

            #Increment running totals
            t_pos = t_pos + batch_t_pos
            f_pos = f_pos + batch_f_pos
            f_neg = f_neg + batch_f_neg
            t_neg = t_neg + batch_t_neg
            batch_num = batch_num + 1

            #Print running results
            print("\t-units = " + str(units_num) + "/" + str(total_units) + " (" + "{0:.4f}".format(100*units_num/total_units) + "% of units fed)")
            #Prints current learning rate
            print("\t-alpha = " + str(sess.run('alpha:0', feed_dict=feed_dict)))
            #Prints current loss
            print("\t-loss  = " + str(sess.run('loss:0', feed_dict=feed_dict)))

            #Print tabulated gal data
            tableGal = Texttable()
            tableGal.set_precision(4)
            tableGal.set_cols_width([11, 7, 7, 7, 7])
            tableGal.set_cols_align(['r', 'r', 'r', 'r', 'r'])
            tableGal.add_rows([
                                ['GAL_PREDs', 'T', 'F', 'T+F', '%'],

                                ['BATCH_' + str(batch_num),
                                batch_t_pos,
                                batch_f_pos,
                                batch_t_pos + batch_f_pos,
                                "{0:.4f}".format(100*batch_t_pos/(batch_t_pos + batch_f_neg) if (batch_t_pos + batch_f_neg != 0) else 100)],

                                ['SESSION',
                                t_pos,
                                f_pos,
                                t_pos + f_pos,
                                "{0:.4f}".format(100*t_pos/(t_pos + f_neg) if (t_pos + f_neg != 0) else 100)]
                            ])
            print(tableGal.draw())

            #Print tabulated nse data
            tableNse = Texttable()
            tableNse.set_precision(4)
            tableNse.set_cols_width([11, 7, 7, 7, 7])
            tableNse.set_cols_align(['r', 'r', 'r', 'r', 'r'])
            tableNse.add_rows([
                                ['NSE_PREDs', 'T', 'F', 'T+F', '%'],

                                ['BATCH_' + str(batch_num),
                                batch_t_neg,
                                batch_f_neg,
                                batch_t_neg + batch_f_neg,
                                "{0:.4f}".format(100*batch_t_neg/(batch_t_neg + batch_f_pos) if (batch_t_neg + batch_f_pos != 0) else 100)],

                                ['SESSION',
                                t_pos,
                                f_pos,
                                t_pos + f_pos,
                                "{0:.4f}".format(100*t_neg/(t_neg + f_pos) if (t_neg + f_pos != 0) else 100)]
                            ])
            print(tableNse.draw())

            #Print tabulated summary
            tableSum = Texttable()
            tableSum.set_precision(4)
            tableSum.set_cols_width([11, 7, 7, 7, 7])
            tableSum.set_cols_align(['r', 'r', 'r', 'r', 'r'])
            tableSum.add_rows([
                                ['ALL_PREDs', 'T', 'F', 'T+F', '%'],

                                ['BATCH_' + str(batch_num),
                                batch_t_pos + batch_t_neg,
                                batch_f_pos + batch_f_neg,
                                batch_t_neg + batch_f_neg + batch_t_pos + batch_f_pos,
                                "{0:.4f}".format(100*(batch_t_pos + batch_t_neg)/len(image_batch)) ],

                                ['SESSION',
                                t_pos + t_neg,
                                f_pos + f_neg,
                                t_neg + f_neg + t_pos + f_pos,
                                "{0:.4f}".format(100*(t_pos + t_neg)/units_num) ]
                            ])
            print(tableSum.draw())
            print("")


            #Ready for new batch
            image_batch = []
            label_batch = []

    #Only optimise & save the graph if in training mode
    if optimise_and_save == 1:
        #Save the slightly more trained graph if in training mode
        print("Saving training modifications made to graph in this run")
        update_model(graph_name, sess, saver)

    #Close tensorflow session
    sess.close()

#Recieves a unit and evaluates it using the graph
def use_evaluation_unit_on_cpu(graph_name, port):
    sock = socket.socket()
    sock.connect(('', port))
    sock.setblocking(0) #Throw an exception when out of data to read (non-blocking)
    timeout = 3         #Five second timeout

    #Make sure graph structure is reset before opening session
    tf.reset_default_graph()

    #Begin a tensorflow session
    sess = tf.Session()

    #Load the graph to be trained & keep the saver for later updating
    saver = restore_model(graph_name, sess)

    #Load image data from socket while there is image data to load
    recieving = True
    while recieving:
        #When ready then recv
        image_input = None
        ready = select.select([sock], [], [], timeout)
        if ready[0]:
            #If WIDTH*HEIGHT*4 units are recieved then this is an image
            image_bytes = sock.recv((WIDTH*HEIGHT)*4)
            image_input = byte_string_to_int_array(image_bytes)
            if len(image_input) != WIDTH*HEIGHT or not image_input:
                #No data found in socket, stop recving
                print("Error: image data not recv'd correctly, finishing early")
                break
        else:
            print("No image data found in socket. Ending recv'ing loop")
            recieving = False

        #If something was recieved from socket
        if recieving:
            #Make the image graph compatible
            image_input = make_compatible(image_input, True)

            #Create a unitary feeder dictionary
            feed_dict_eval = {  'images:0': [image_input],
                                'is_training:0': False}

            #Get the prediction
            pred = sess.run(OUTPUT_NAME + ':0', feed_dict=feed_dict_eval)[0]
            print(pred)

    #Close tensorflow session
    sess.close()

#Must occur pre NCS usage, creates a version of the .meta file without any
#training placeholders (otherwise will fail)
def compile_for_ncs(graph_name):
    #Create a graph, if graph is any larger then network will
    #not be Movidius NCS compatible (reason unknown)
    new_graph(id=graph_name,      #Id/name
              filter_sizes=FILTER_SIZES,  #Convolutional layer filter sizes in pixels
              num_filters=NUM_FILTERS, #Number of filters in each Convolutional layer
              fc_sizes=FC_SIZES,    #Number of neurons in fully connected layer
              for_training=False)   #Gets rid of placeholders and training structures
                                    #that aren't needed for NCS

    #Prepare to save all this stuff
    saver = tf.train.Saver(tf.global_variables())

    #A session is required
    sess = tf.Session()

    #Initialise no placeholder architecture
    sess.run(tf.global_variables_initializer())
    sess.run(tf.local_variables_initializer())

    #Load in weights from trained graph
    #saver.restore(sess, GRAPHS_FOLDER + "/" + graph_name + "/" + graph_name)

    #Save without placeholders
    saver.save(sess, GRAPHS_FOLDER + "/" + graph_name + "/" + graph_name + "-for-ncs")

    #Finish session
    sess.close()

    #Compile graph with subprocess for NCS
    #Example: 'mvNCCompile graphs/test-graph/test-graph.meta -in=images -on=predictor -o=./graphs/test-graph/test-graph-for-ncs.graph'
    subprocess.call([
        'mvNCCompile',
        (GRAPHS_FOLDER + "/" + graph_name + "/" + graph_name + "-for-ncs.meta"),
        "-in=" + INPUT_NAME,
        "-on=" + OUTPUT_NAME,
        "-o=" + GRAPHS_FOLDER + "/" + graph_name + "/" + graph_name + "-for-ncs.graph",
        "-is", str(WIDTH), str(HEIGHT)
    ],
    stdout=open(os.devnull, 'wb')); #Suppress output

#Boots up one NCS, loads a compiled version of the graph onto it and begins
#running inferences on it. Supports inferencing a 3d area that must be supplied
def run_evaluation_client(graph_name, port):
    #Compile a copy of the graph for the NCS architecture
    compile_for_ncs(graph_name)

    #Load the compiled graph
    graph_file = None;
    filepath = GRAPHS_FOLDER + "/" + graph_name + "/" + graph_name + "-for-ncs.graph"
    with open(filepath, mode='rb') as f:
        #Read it in
        graph_file = f.read()

    #Ensure there is at least one NCS
    device_list = mvnc.EnumerateDevices()
    if len(device_list) == 0:
        print("Error: no NCS devices detected, exiting")
        sys.exit()
    elif len(device_list) == 1:
        print("Forking " + str(len(device_list)) + " additional NCS management child")
    else:
        print("Forking " + str(len(device_list)) + " additional NCS management children")

    #Fork a child process for each available NCS
    for d in range(len(device_list)):
        #Try to open an NCS for each child
        device = mvnc.Device(device_list[d])
        device.OpenDevice()

        #Fork child
        pid = os.fork()

        #Behaviour changes per process
        if pid > 0:
            #Parent, if not a process for each device then keep forking
            if d == len(device_list) - 1:
                #Enough children, wait for them each to die
                dead_children = 0
                while(dead_children < len(device_list)):
                    os.wait()   #Waits for one child to die
                    dead_children = dead_children + 1

                #Once dead children have been gathered then data can be processed
                print("All children dead, processing results")

        elif pid == 0:
            #Child, connect to socket
            print("Connecting child evaluation process handling NCS:" + str(d) + " to port " + str(port))
            sock = socket.socket()
            sock.connect(('', port))
            sock.setblocking(0) #Throw an exception when out of data to read (non-blocking)
            timeout = 3         #Five second timeout

            #Allocate the compiled graph onto the device and get a reference
            #for later deallocation
            graph_ref = device.AllocateGraph(graph_file)

            #Announce about to enter reception loop
            print("Process controlling NCS:" + str(d) + " entering reception loop")

            #Load image data from socket while there is image data to load
            recieving = True
            while recieving:
                #When ready then recv
                image_input = None
                ready = select.select([sock], [], [], timeout)
                if ready[0]:
                    #If WIDTH*HEIGHT*4 units are recieved then this is an image
                    image_bytes = sock.recv((WIDTH*HEIGHT)*4)
                    image_input = byte_string_to_int_array(image_bytes)
                    if len(image_input) != WIDTH*HEIGHT or not image_input:
                        #No data found in socket, stop recving
                        print("Error: image data not recv'd correctly, finishing early")
                        break
                else:
                    print("No image data found in socket. Closing NCS:" + str(d))
                    recieving = False

                #If something was recieved from socket
                if recieving:
                    #Make the image graph compatible
                    image_input = make_compatible(image_input, True)
                    eval_feed = np.reshape(image_input, (1, WIDTH, HEIGHT, 3))

                    print("NCS:")

                    #Get the graph's prediction
                    if graph_ref.LoadTensor(image_input, "image"):
                        #Get output of graph
                        output, userobj = graph_ref.GetResult()
                        pred = output[0]
                        print(output)
                        if 0.5 < pred and pred <= 1:
                            print("GALAXY (" + "{0:.4f}".format(pred*100) + "%)")
                        elif 0 <= pred and pred <= 0.5:
                            print("NOISE (" + "{0:.4f}".format((1 - pred)*100) + "%)")
                        else:
                            print("Error: '" + OUTPUT_NAME + "' output was invalid: " + str(pred))
                    else:
                        print("Error: cannot evaluate output of neural network, continuing")
                        continue

            #Finished recieving, deallocate the graph from the device
            graph_ref.DeallocateGraph()

            #Close opened device
            device.CloseDevice()

            #Kill child
            sys.exit()
        else:
            print("Error: couldn't successfully fork a child for NCS " + str(d))

#Plots the convolutional filter-weights/kernel for a given layer using matplotlib
def plot_conv_weights(graph_name, scope):
    #Make sure graph structure is reset before opening session
    tf.reset_default_graph()

    #Begin a tensorflow session
    sess = tf.Session()

    #Load the graph to be trained & keep the saver for later updating
    saver = restore_model(graph_name, sess)

    #Get the weights
    w = None
    for v in tf.all_variables():
        if v.name == scope + '/kernel:0':
            w = sess.run(v)

    #Get the lowest and highest values for the weights to scale colour intensity
    #across filters
    w_min = np.min(w)
    w_max = np.max(w)

    #Number of filters used in the conv. layer
    num_filters = w.shape[3]

    #Create figure with a grid of sub-plots (4xY)
    fig, axes = plt.subplots(4, math.ceil(num_filters/4))

    #Plot all the filter-weights.
    for i, ax in enumerate(axes.flat):
        #Only plot the valid filter-weights.
        if i < num_filters:
            #See new_conv_layer() for details on the format
            #of this 4-dim tensor
            img = w[:, :, 0, i] #Get first depth

            #Plot image
            ax.imshow(img, vmin=w_min, vmax=w_max,
                      interpolation='nearest', cmap='PiYG')

        #Remove ticks from the plot.
        ax.set_xticks([])
        ax.set_yticks([])

    fig.savefig("output/" + scope + "_kernel")

    #Explicity close figure for memory usage
    plt.close(fig)

    #Close tensorflow session (no need to save)
    sess.close()

#Initialises a fresh graph and stores it for later training
def new_graph(id,             #Unique identifier for saving the graph
              filter_sizes,   #Filter dims for each convolutional layer (kernals)
              num_filters,    #Number of filters for each convolutional layer
              fc_sizes,       #Number of neurons in fully connected layers
              for_training):  #The Movidius NCS' are picky and won't resolve unknown
                              #placeholders. For loading onto the NCS these, and other
                              #training structures (optimizer, dropout, batch normalisation)
                              #all must go

    #Create computational graph to represent the neural net:
    print("Creating new graph: '" + str(id) + "'")
    print("\t*Structure details:")

    #Movidius NCS requires an RGB 'colour' image, even though our inputs are
    #grayscale
    channels = 3

    #INPUT
    #Placeholders serve as variable input to the graph (can be changed when run)
    #Following placeholder takes 30x30 grayscale images as tensors
    #(must be float32 for convolution and must be 4D for tensorflow)
    images = tf.placeholder(tf.float32, shape=[None, WIDTH, HEIGHT, channels], name='images')
    print("\t\t-Placeholder '" + images.name + "': " + str(images))

    if for_training:
        #and supervisory signals which are boolean (is or is not a galaxy)
        labels = tf.placeholder(tf.float32, shape=[None, 1], name='labels')
        print("\t\t-Placeholder '" + labels.name + "': " + str(labels))
        #Whether or not we are training (for batch normalisation)
        is_training = tf.placeholder(tf.bool, name='is_training')

    #CONVOLUTIONAL LAYERS
    #These convolutional layers sequentially take inputs and create/apply filters
    #to these inputs to create outputs. They create weights and biases that will
    #be optimised during graph execution. They also down-sample (pool) the image
    #after doing so. Filters are created in accordance with the arguments to this
    #function
    layer_conv_0 = tf.layers.conv2d(
        inputs=images,
        filters=num_filters[0],
        kernel_size=filter_sizes[0],
        use_bias=True,
        bias_initializer=tf.truncated_normal_initializer(mean=0, stddev=0.05),
        trainable=True,
        name="conv_0"
    )
    print("\t\t-Convolutional 0: " + str(layer_conv_0))

    #if for_training:
        #Apply batch normalisation after ReLU
    #layer_conv0 = tf.layers.batch_normalization(layer_conv0, training=is_training)

    #layer 2 takes layer 1's output
    layer_conv_1 = tf.layers.conv2d(
        inputs=layer_conv_0,
        filters=num_filters[1],
        kernel_size=filter_sizes[1],
        use_bias=True,
        bias_initializer=tf.truncated_normal_initializer(mean=0, stddev=0.05),
        trainable=True,
        name="conv_1"
    )
    print("\t\t-Convolutional 1: " + str(layer_conv_1))

    #if for_training:
        #Apply batch normalisation after ReLU
    #layer_conv1 = tf.layers.batch_normalization(layer_conv1, training=is_training)

    #Fully connected layers only take 2D tensors so above output must be
    #flattened from 4d
    num_features = (layer_conv_1.get_shape())[1:4].num_elements()
    layer_flat = tf.reshape(layer_conv_1, [-1, num_features])
    print("\t\t-Flattener 0: " + str(layer_flat))

    #FULLY CONNECTED LAYERS
    #These fully connected layers create new weights and biases and matrix
    #multiply the weights with the inputs, then adding the biases. They then
    #apply a ReLU function before returning the layer. These weights and biases
    #are learned during execution
    layer_fc_0 = tf.layers.dense(
        inputs=layer_flat,    #Will be auto flattened
        units=fc_sizes[0],
        activation=tf.nn.relu,
        use_bias=True,
        bias_initializer=tf.truncated_normal_initializer(mean=0, stddev=0.05),
        trainable=True,
        name="fc_0"
    )
    print("\t\t-Fully connected 0: " + str(layer_fc_0))

    #if for_training:
        #Apply batch normalisation after ReLU
        #layer_fc_0 = tf.layers.batch_normalization(layer_fc_0, training=is_training)

    layer_fc_1 = tf.layers.dense(
        inputs=layer_fc_0,
        units=fc_sizes[1],
        activation=tf.nn.relu,
        use_bias=True,
        bias_initializer=tf.truncated_normal_initializer(mean=0, stddev=0.05),
        trainable=True,
        name="fc_1"
    )
    print("\t\t-Fully connected 1: " + str(layer_fc_1))

    #if for_training:
        #Apply batch normalisation after ReLU
        #layer_fc_1 = tf.layers.batch_normalization(layer_fc_1, training=is_training)

    layer_fc_2 = tf.layers.dense(
        inputs=layer_fc_1,
        units=1,
        activation=None,
        use_bias=True,
        bias_initializer=tf.truncated_normal_initializer(mean=0, stddev=0.05),
        trainable=True,
        name="fc_2"
    )
    print("\t\t-Fully connected 2: " + str(layer_fc_2))

    #PREDICTION DETAILS
    #Final fully connected layer suggests prediction (these structures are added to
    #collections for ease of access later on)
    print("\t*Prediction details:")

    #Softmax it to get a probability (Greater than 0.5 was used earlier to get
    #prediction as a boolean, but this is unsupported by Movidius ncsdk at this time)
    prediction = tf.nn.sigmoid(layer_fc_2, name='predictor')
    print("\t\t-Class prediction: " + str(prediction))

    #Backpropogation details only required when training
    if for_training:
        print("\t*Backpropagation details:")

        #COST FUNCTION
        #Cost function is cross entropy (+ve and approaches zero as the model output
        #approaches the desired output
        cross_entropy = tf.nn.sigmoid_cross_entropy_with_logits(logits=layer_fc_2,
                                                                labels=labels,
                                                                name='sigmoid_cross_entropy')
        print("\t\t-Cross entropy: " + str(cross_entropy))
        loss = tf.reduce_mean(cross_entropy, name='loss')
        print("\t\t-Loss (reduced mean cross entropy): " + str(loss))

        #OPTIMISATION FUNCTION
        #Optimisation function will have a decaying learning rate for bolder retuning
        #at the beginning of the trainig run
        global_step = tf.Variable(0, trainable=False)   #Incremented per batch
        init_alpha = 0.001  #Ideally want to go down to 1e-4
        decay_base = 0.99   #alpha = alpha*decay_base^(global_step/decay_steps)
        decay_steps = 64    #With data set of 60000, this should get us to 0.0001
        alpha = tf.train.exponential_decay( init_alpha,
                                            global_step, decay_steps, decay_base,
                                            name='alpha')

        print("\t\t-Learning rate: " + str(alpha))

        #Optimisation function to Optimise cross entropy will be Adam optimizer
        #(advanced gradient descent)
        #Require the following extra ops due to batch normalisation
        extra_update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(extra_update_ops):
            optimiser = (
                tf.train.AdamOptimizer(learning_rate=init_alpha)
                .minimize(loss, global_step=global_step)    #Decay learning rate
                                                            #by incrementing global step
                                                            #once per batch
            )

        print("\t\t-Optimiser: " + str(optimiser.name))

#Wraps the above to make a basic convolutional neural network for binary
#image classification
def new_basic_training_graph(id):
    #Create a graph, if graph is any larger then network will
    #not be Movidius NCS compatible (reason unknown)
    new_graph(id,      #Id/name
              filter_sizes=FILTER_SIZES,    #Convolutional layer filter sizes in pixels
              num_filters=NUM_FILTERS,      #Number of filters in each Convolutional layer
              fc_sizes=FC_SIZES,            #Number of neurons in fully connected layer
              for_training=True)

    #Save it in a tensorflow session
    sess = tf.Session()
    save_model_as_meta(id, sess)
    sess.close()

#Restores the model (graph and variables) from a supplied filepath
def restore_model(id, sess):
    #Location is in graphs/id
    filepath = GRAPHS_FOLDER + "/" + id + "/" + id

    #Load from file
    saver = tf.train.import_meta_graph(filepath + ".meta")  #Graph structure
    saver.restore(sess, filepath)                       #Variables

    #Return the saver
    return saver

#Updates a model (used in training)
def update_model(id, sess, saver):
    #Remove old if it exists (don't complain if it doesn't)
    shutil.rmtree(GRAPHS_FOLDER + "/" + id)

    #File is named id in id folder
    filepath = GRAPHS_FOLDER + "/" + id + "/" + id

    #Saving operation
    saver.save(sess, filepath)                      #Variables
    saver.export_meta_graph(filepath + ".meta")     #Graph structure

#Saves the current model (graph and variables) to a supplied filepath
def save_model_as_meta(id, sess):
    #Remove old if it exists (don't complain if it doesn't)
    shutil.rmtree(GRAPHS_FOLDER + "/" + id, True)

    #File is named id in id folder
    filepath = GRAPHS_FOLDER + "/" + id + "/" + id

    #Initialise if required
    sess.run(tf.global_variables_initializer())
    sess.run(tf.local_variables_initializer())

    #Saving operation
    saver = tf.train.Saver(max_to_keep=1)           #Keep only one copy
    saver.save(sess, filepath)                      #Variables
    saver.export_meta_graph(filepath + ".meta")     #Graph structure

#Export's the current model (graph and variables) to a pb MVNCcompile-able
#serialized graph file
def save_model_as_pb(id, sess):
    #Announce
    print("Exporting model to: '" + GRAPHS_FOLDER + "/" + id + "/" + id + ".pb' ")

    #Get graph
    graph = sess.graph_def

    #Notify of placeholder names to use when compiling for evaluation
    #print("& including the following operations:")
    #ops = sess.graph.get_operations()
    #print(ops)

    #print(sess.graph.get_tensor_by_name("images:0"))

    #Export to specified file
    with open(GRAPHS_FOLDER + "/" + id + "/" + id + ".pb", 'wb') as f:
        f.write(graph.SerializeToString())
