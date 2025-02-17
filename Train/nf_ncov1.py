'''
First training file using new format (check the prediction).
Can be trained using the *latest* deepjetcore (there was a minor change to allow for an arbitrary number of predictions for keras models).
A dataset can be found here: /eos/home-j/jkiesele/DeepNtuples/HGCal/Sept2020_19_production_1x1
'''
import tensorflow as tf
# from K import Layer
import numpy as np
from tensorflow.keras.layers import BatchNormalization, Dropout, Add
from LayersRagged  import RaggedConstructTensor
from GravNetLayersRagged import ProcessFeatures,SoftPixelCNN, RaggedGravNet, DistanceWeightedMessagePassing
from tensorflow.keras.layers import Reshape, Dense, Concatenate, GaussianDropout, Dropout
from DeepJetCore.modeltools import DJCKerasModel
from DeepJetCore.training.training_base import training_base
from tensorflow.keras import Model


from DeepJetCore.modeltools import fixLayersContaining
# from tensorflow.keras.models import load_model
from DeepJetCore.training.training_base import custom_objects_list

# from tensorflow.keras.optimizer_v2 import Adam

from plotting_callbacks import plotEventDuringTraining
from ragged_callbacks import plotRunningPerformanceMetrics
from DeepJetCore.DJCLayers import ScalarMultiply, SelectFeatures, ReduceSumEntirely, StopGradient

from clr_callback import CyclicLR
from lossLayers import LLFullObjectCondensation, LLClusterCoordinates

from model_blocks import create_outputs

from Layers import EdgeConvStatic,DistanceWeightedMessagePassing,SortAndSelectNeighbours,NeighbourCovariance,NeighbourApproxPCA,ReluPlusEps,NormalizeInputShapes,NeighbourCovariance,LocalDistanceScaling,LocalClusterReshapeFromNeighbours,GraphClusterReshape, SortAndSelectNeighbours, LLLocalClusterCoordinates,DistanceWeightedMessagePassing,CollectNeighbourAverageAndMax,CreateGlobalIndices, LocalClustering, SelectFromIndices, MultiBackGather, KNN, MessagePassing
from datastructures import TrainData_NanoML 
td=TrainData_NanoML()

def gravnet_model(Inputs, feature_dropout=-1., addBackGatherInfo=True):
    
    
    ######## pre-process all inputs and create global indices etc. No DNN actions here
    
    feat,  t_idx, t_energy, t_pos, t_time, t_pid, row_splits = td.interpretAllModelInputs(Inputs)
    feat,  t_idx, t_energy, t_pos, t_time, t_pid = NormalizeInputShapes()(
        [feat,  t_idx, t_energy, t_pos, t_time, t_pid]
        )
    
    orig_t_idx, orig_t_energy, orig_t_pos, orig_t_time, orig_t_pid, orig_row_splits = t_idx, t_energy, t_pos, t_time, t_pid, row_splits
    gidx_orig = CreateGlobalIndices()(feat)
    
    _, row_splits = RaggedConstructTensor()([feat, row_splits])
    rs = row_splits
    
    feat_norm = ProcessFeatures()(feat)#get rid of unit scalings, almost normalise
    feat_norm = BatchNormalization(momentum=0.6)(feat_norm)
    x=feat_norm
    
    energy = SelectFeatures(0,1)(feat)
    time = SelectFeatures(8,9)(feat)
    orig_coords = SelectFeatures(5,8)(feat_norm)
    
    ######## create output lists
    
    allfeat=[]
    
    backgatheredids=[]
    gatherids=[]
    backgathered = []
    backgathered_coords = []
    
    ####### create simple first coordinate transformation explicitly (time critical)

    coords=orig_coords
    coords = Dense(16,activation='elu')(coords)
    coords = Dense(32,activation='elu')(coords)
    coords = Dense(3,use_bias=False)(coords)
    coords = ScalarMultiply(0.1)(coords)
    coords = Add()([coords, orig_coords])
    coords = Dense(3,use_bias=False,kernel_initializer=tf.keras.initializers.identity())(coords)
    
    first_coords = coords
    
    ###### apply one gravnet-like transformation (explicit here because we have created coords by hand) ###

    nidx, dist = KNN(K=48)([coords,rs])
    x_mp = DistanceWeightedMessagePassing([32])([x,nidx,dist])
    
    first_nidx = nidx
    first_dist = dist
    
    ###### collect information about the surrounding energy and time distributions per vertex ###

    ncov = NeighbourCovariance()([coords,ReluPlusEps()(Concatenate()([energy,time])),nidx])
    ncov = BatchNormalization(momentum=0.6)(ncov)
    ncov = Dense(64, activation='elu',name='pre_dense_ncov_a')(ncov)
    ncov = Dense(32, activation='elu',name='pre_dense_ncov_b')(ncov)
    
    ##### put together and process ####
    
    x = Concatenate()([x,x_mp,ncov,coords])
    x = Dense(64, activation='elu',name='pre_dense_a')(x)
    x = BatchNormalization(momentum=0.6)(x)
    x = Dense(32, activation='elu',name='pre_dense_b')(x)
    
    ####### add first set of outputs to output lists
    
    allfeat.append(x)
    backgathered_coords.append(coords)
    
    total_iterations=5
    
    sel_gidx = gidx_orig
    
    for i in range(total_iterations):
        
        ###### reshape the graph to fewer vertices ####
        
        hier = Dense(1)(x)
        dist = LocalDistanceScaling()([dist, Dense(1)(x)])
        
        x_cl, rs, bidxs, sel_gidx, energy, x, t_idx,coords = LocalClusterReshapeFromNeighbours(
                 K=6, 
                 radius=0.5, #doesn't really have an effect because of local distance scaling
                 print_reduction=True, 
                 loss_enabled=True, 
                 loss_scale = 4., 
                 loss_repulsion=0.5,
                 print_loss=True,
                 name='clustering_'+str(i)
                 )([x, dist, hier, nidx, rs, sel_gidx, energy, x, t_idx, coords, t_idx])#last is truth index used by layer
        
        gatherids.append(bidxs)
        
        if i or True:
            x_cl_rs = Reshape([-1, x.shape[-1]])(x_cl) #get to shape V x K x F
            xec = EdgeConvStatic([32,32,32],name="ec_static_"+str(i))(x_cl_rs)
            x_cl = Concatenate()([x,xec])
        
        ### explicitly sum energy and re-add to features
        
        energy = ReduceSumEntirely()(energy)
        n_energy = BatchNormalization(momentum=0.6)(energy)
        x = Concatenate()([x_cl,n_energy])
        
        
        x = Dense(128, activation='elu',name='dense_clc0_'+str(i))(x)
        x = Dense(64, activation='relu',name='dense_clc1_'+str(i))(x)
        #notice last relu for feature weighting later
        
        
        x_gn, coords, nidx, dist = RaggedGravNet(n_neighbours = 32+16*i,
                                                 n_dimensions= 3,
                                                 n_filters = 64+16*i,
                                                 n_propagate = 64,
                                                 return_self=True)([Concatenate()([coords,x]), 
                                                                    rs])
        
        
         
        ### add neighbour summary statistics
                        
        x_ncov = NeighbourCovariance()([coords,ReluPlusEps()(x),nidx])
        x_ncov = Dense(128, activation='elu',name='dense_ncov_a_'+str(i))(x_ncov)
        x_ncov = BatchNormalization(momentum=0.6)(x_ncov)
        x_ncov = Dense(64, activation='elu',name='dense_ncov_b_'+str(i))(x_ncov)
        x = Concatenate()([x,x_ncov,x_gn])
        
        ### with all this information perform a few message passing steps
        
        x_mp = MessagePassing([32,32,16,16,8,8])([x,nidx])
        x_mp = Dense(64, activation='elu',name='dense_mpc_'+str(i))(x_mp)
        x = Concatenate()([x,x_mp])
        
        ##### prepare output of this iteration
                                            
        x = Dense(128, activation='elu',name='dense_out_a_'+str(i))(x)
        x = BatchNormalization(momentum=0.6)(x)
        x = Dense(64, activation='elu',name='dense_out_b_'+str(i))(x)
        x = BatchNormalization(momentum=0.6)(x)
        
        #### compress further for output, but forward fill 64 feature x to next iteration
        
        x_r = Dense(8+16*i, activation='elu',name='dense_out_c_'+str(i))(x)
        #coords_nograd = StopGradient()(coords)
        #x_r = Concatenate()([coords_nograd,x_r]) ## add coordinates, might come handy for cluster space
        
        if i >= total_iterations-1:
            energy = MultiBackGather()([energy, gatherids])#assign energy sum to all cluster components
        
        allfeat.append(MultiBackGather()([x_r, gatherids]))
        backgatheredids.append(MultiBackGather()([sel_gidx, gatherids]))
        backgathered_coords.append(MultiBackGather()([coords, gatherids]))      
        
        
    x = Concatenate(name='allconcat')(allfeat)
    #x = Dropout(0.2)(x)
    x_mp = DistanceWeightedMessagePassing([32,32,32])([x,first_nidx,first_dist])
    x = Concatenate()([x,x_mp])
    
    x = Dense(128, activation='elu', name='alldense')(x)
    # TO BE ADDED WITH E LOSS x = Concatenate()([x,energy])
    #x = Dropout(0.2)(x)
    x = BatchNormalization(momentum=0.6)(x)
    x = Dense(64, activation='elu')(x)
    x = BatchNormalization(momentum=0.6)(x)
    x = Dense(64, activation='elu')(x)

    pred_beta, pred_ccoords, pred_energy, pred_pos, pred_time, pred_id = create_outputs(x,feat)
    
    #
    #
    # double scale phase transition with linear beta + qmin
    #  -> more high beta points, but: payload loss will still scale one 
    #     (or two, but then doesn't matter)
    #
    
    pred_beta = LLFullObjectCondensation(print_loss=True,
                                         energy_loss_weight=0.,
                                         position_loss_weight=0., #seems broken
                                         timing_loss_weight=0.,#1e-3,
                                         beta_loss_scale=1.,
                                         repulsion_scaling=1.,
                                         q_min=1.5,
                                         prob_repulsion=True,
                                         phase_transition=0,
                                         phase_transition_double_weight=False,
                                         alt_potential_norm=True,
                                         cut_payload_beta_gradient=False
                                         )([pred_beta, pred_ccoords, pred_energy, 
                                            pred_pos, pred_time, pred_id,
                                            orig_t_idx, orig_t_energy, orig_t_pos, orig_t_time, orig_t_pid,
                                            row_splits])


    return Model(inputs=Inputs, outputs=[pred_beta, 
                                         pred_ccoords,
                                         pred_energy, 
                                         pred_pos, 
                                         pred_time, 
                                         pred_id,
                                         rs]+backgatheredids+backgathered_coords)





train = training_base(testrun=False, resumeSilently=True, renewtokens=False)


if not train.modelSet():

    train.setModel(gravnet_model)
    train.setCustomOptimizer(tf.keras.optimizers.Nadam())

    train.compileModel(learningrate=1e-4,
                       loss=None)
    
    print(train.keras_model.summary())
    #exit()

verbosity = 2
import os

from plotting_callbacks import plotClusteringDuringTraining, plotGravNetCoordsDuringTraining

samplepath=train.val_data.getSamplePath(train.val_data.samples[0])
publishpath = 'jkiesele@lxplus.cern.ch:/eos/home-j/jkiesele/www/files/HGCalML_trainings/'+os.path.basename(os.path.normpath(train.outputDir))

cb = [plotClusteringDuringTraining(
           use_backgather_idx=7+i,
           outputfile=train.outputDir + "/plts/sn"+str(i)+'_',
           samplefile=  samplepath,
           after_n_batches=300,
           on_epoch_end=False,
           publish=publishpath+"_cl_"+str(i),
           use_event=0) 
    for i in [0,4]]

cb += [   
    plotEventDuringTraining(
            outputfile=train.outputDir + "/plts2/sn0",
            samplefile=samplepath,
            after_n_batches=300,
            batchsize=200000,
            on_epoch_end=False,
            publish = publishpath+"_event_"+ str(0),
            use_event=0)
    
    ]

cb += [   
    plotGravNetCoordsDuringTraining(
            outputfile=train.outputDir + "/coords_"+str(i)+"/coord_"+str(i),
            samplefile=samplepath,
            after_n_batches=300,
            batchsize=200000,  
            on_epoch_end=False,
            publish = publishpath+"_event_"+ str(0),
            use_event=0,
            use_prediction_idx=i,
            )
    for i in  range(12,18) #between 16 and 21
    ]
learningrate = 5e-3
nbatch = 120000 #quick first training with simple examples = low # hits

train.compileModel(learningrate=learningrate,
                          loss=None,
                          metrics=None)


model, history = train.trainModel(nepochs=4,
                                  run_eagerly=True,
                                  batchsize=nbatch,
                                  extend_truth_list_by = len(train.keras_model.outputs)-2, #just adapt truth list to avoid keras error (no effect on model)
                                  batchsize_use_sum_of_squares=False,
                                  checkperiod=1,  # saves a checkpoint model every N epochs
                                  verbose=verbosity,
                                  backup_after_batches=100,
                                  additional_callbacks=
                                  [CyclicLR (base_lr = learningrate/3.,
                                  max_lr = learningrate,
                                  step_size = 20)]+cb)

#print("freeze BN")
#for l in train.keras_model.layers:
#    if isinstance(l, BatchNormalization):
#        l.trainable=False
#    if 'GravNetLLLocalClusterLoss' in l.name:
#        l.active=False
        
#also stop GravNetLLLocalClusterLoss* from being evaluated

learningrate = 1e-4
train.compileModel(learningrate=learningrate,
                          loss=None,
                          metrics=None)

model, history = train.trainModel(nepochs=121,
                                  run_eagerly=True,
                                  batchsize=nbatch,
                                  extend_truth_list_by = len(train.keras_model.outputs)-2, #just adapt truth list to avoid keras error (no effect on model)
                                  batchsize_use_sum_of_squares=False,
                                  checkperiod=1,  # saves a checkpoint model every N epochs
                                  verbose=verbosity,
                                  backup_after_batches=100,
                                  additional_callbacks=
                                  [CyclicLR (base_lr = learningrate,
                                  max_lr = learningrate*2.,
                                  step_size = 100)]+cb)

