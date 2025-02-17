import tensorflow as tf
from object_condensation import oc_loss
from betaLosses import obj_cond_loss
import time


def huber(x, d):
    losssq  = x**2   
    absx = tf.abs(x)                
    losslin = d**2 + 2. * d * (absx - d)
    return tf.where(absx < d, losssq, losslin)

class LossLayerBase(tf.keras.layers.Layer):
    """Base class for HGCalML loss layers.
    
    Use the 'active' switch to switch off the loss calculation.
    This needs to be done by hand, and is not handled by the TF 'training' flag, since
    it might be desirable to 
     (a) switch it off during training, or 
     (b) calculate the loss also during inference (e.g. validation)
     
     
    The 'scale' argument determines a global sale factor for the loss. 
    """
    
    def __init__(self, active=True, scale=1., 
                 print_loss=False,
                 return_lossval=False, **kwargs):
        super(LossLayerBase, self).__init__(**kwargs)
        
        self.active = active
        self.scale = scale
        self.print_loss = print_loss
        self.return_lossval=return_lossval
        
    def get_config(self):
        config = {'active': self.active ,
                  'scale': self.scale,
                  'print_loss': self.print_loss,
                  'return_lossval': self.return_lossval}
        base_config = super(LossLayerBase, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))
    
    def call(self, inputs):
        lossval = tf.constant([0.],dtype='float32')
        if self.active:
            lossval = self.scale * self.loss(inputs)
            if not self.return_lossval:
                self.add_loss(lossval)
        if self.return_lossval:
            return inputs[0], lossval
        else:
            return inputs[0]
    
    def loss(self, inputs):
        '''
        Overwrite this function in derived classes.
        Input: always a list of inputs, the first entry in the list will be returned, and should be the features.
        The rest is free (but will probably contain the truth somewhere)
        '''
        return tf.constant(0.,dtype='float32')

    def compute_output_shape(self, input_shapes):
        if self.return_lossval:
            return input_shapes[0], (None,)
        else:
            return input_shapes[0]


#naming scheme: LL<what the layer is supposed to do>
class LLClusterCoordinates(LossLayerBase):
    '''
    Cluster using truth index and coordinates
    '''
    def __init__(self, repulsion_contrib=0.5, **kwargs):
        self.repulsion_contrib=repulsion_contrib
        assert repulsion_contrib <= 1. and repulsion_contrib>= 0.
        
        if 'dynamic' in kwargs:
            super(LLClusterCoordinates, self).__init__(**kwargs)
        else:
            super(LLClusterCoordinates, self).__init__(dynamic=True,**kwargs)

    def loss(self, inputs):
        
        use_avg_cc=True
        coords, truth_indices, row_splits, beta_like = None, None, None, None
        if len(inputs) == 3:
            coords, truth_indices, row_splits = inputs
        elif len(inputs) == 4:
            coords, truth_indices, beta_like, row_splits = inputs
            use_avg_cc = False
        else:
            raise ValueError("LLClusterCoordinates requires 3 or 4 inputs")

        zeros = tf.zeros_like(coords[:,0:1])
        if beta_like is None:
            beta_like = zeros+1./2.
        else:
            beta_like = tf.nn.sigmoid(beta_like)
            beta_like = tf.stop_gradient(beta_like)#just informing, no grad # 0 - 1
            beta_like = 0.1* beta_like + 0.5 #just a slight scaling
        
        #this takes care of noise through truth_indices < 0
        V_att, V_rep,_,_,_,_=oc_loss(coords, beta_like, #beta constant
                truth_indices, row_splits, 
                zeros, zeros,Q_MIN=1.0, S_B=0.,energyweights=None,
                use_average_cc_pos=use_avg_cc,payload_rel_threshold=0.01)
        
        att = (1.-self.repulsion_contrib)*V_att
        rep = self.repulsion_contrib*V_rep
        lossval = att + rep
        if self.print_loss:
            print(self.name, lossval.numpy(), 'att loss:', att.numpy(), 'rep loss:',rep.numpy())
        return lossval

    def get_config(self):
        config = { 'repulsion_contrib': self.repulsion_contrib }
        base_config = super(LLClusterCoordinates, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class LLLocalClusterCoordinates(LossLayerBase):
    '''
    Cluster using truth index and coordinates
    Inputs: distances, hierarchy tensor, neighbour indices, truth_indices
    
    The loss will be calculated w.r.t. the reference vertex at position [:,0] in the neighbour
    indices unless add_self_reference is set to True. In this case, the vertex at position i will 
    be used for all neighbours [i,:] as reference vertex
    (for GravNet set add_self_reference=True, for local clustering set it to False because the 
    reference vertex is already included in the indices)
    
    '''
    def __init__(self, add_self_reference, repulsion_contrib=0.5, **kwargs):
        self.repulsion_contrib=repulsion_contrib
        self.add_self_reference=add_self_reference
        assert repulsion_contrib <= 1. and repulsion_contrib>= 0.
        
        if 'dynamic' in kwargs:
            super(LLLocalClusterCoordinates, self).__init__(**kwargs)
        else:
            super(LLLocalClusterCoordinates, self).__init__(dynamic=kwargs['print_loss'],**kwargs)

    @staticmethod
    def raw_loss(distances, hierarchy, neighbour_indices, truth_indices,
                 add_self_reference, repulsion_contrib, print_loss, name):
        
        
        hierarchy = (tf.nn.sigmoid(hierarchy)+1.)/2.
        #make neighbour_indices TF compatible (replace -1 with own index)
        own = tf.expand_dims(tf.range(tf.shape(truth_indices)[0],dtype='int32'),axis=1)
        neighbour_indices = tf.where(neighbour_indices<0, own, neighbour_indices) #does broadcasting to the righ thing here?
        
        #reference might already be there, but just to be sure
        if add_self_reference:
            neighbour_indices = tf.concat([own,neighbour_indices],axis=-1)
        else: #remove self-distance
            distances = distances[:,1:]
            
        neighbour_indices = tf.expand_dims(neighbour_indices,axis=2)#tf like
        
        firsttruth = truth_indices #[,tf.squeeze(tf.gather_nd(truth_indices, neighbour_indices[:,0:1]),axis=2)
        neightruth = tf.squeeze(tf.gather_nd(truth_indices, neighbour_indices[:,1:] ),axis=2)
        
        #distances are actually distances**2
        expdist = tf.exp(- 3. * distances)
        att_proto = (1.-repulsion_contrib)* (1.-expdist)  #+ 0.01*distances  #mild attractive to help learn
        att_proto = tf.where(truth_indices<0, att_proto*0.001, att_proto) #milder term for noise
        
        rep_proto = repulsion_contrib * expdist #- 0.01 * distances
        
        
        potential = tf.where(firsttruth==neightruth, att_proto, rep_proto)
        potential = hierarchy**2 * tf.reduce_mean(potential, axis=1, keepdims=True)
        potential = tf.reduce_mean(potential)
        
        penalty = 1. - hierarchy
        penalty = tf.reduce_mean(penalty)
        
        lossval = penalty + potential
        
        if print_loss:
            if hasattr(lossval, "numpy"):
                print(name, lossval.numpy(), 'potential', potential.numpy(), 'penalty',penalty.numpy())
            else:
                tf.print(name, lossval, 'potential',potential, 'penalty',penalty)
        return lossval
        
    def loss(self, inputs):
        distances, hierarchy, neighbour_indices, truth_indices = inputs
        return LLLocalClusterCoordinates.raw_loss(distances, hierarchy, neighbour_indices, 
                                                  truth_indices, 
                                                  self.add_self_reference, 
                                                  self.repulsion_contrib, 
                                                  self.print_loss, 
                                                  self.name)

    def get_config(self):
        config = {'add_self_reference': self.add_self_reference,
                  'repulsion_contrib': self.repulsion_contrib }
        base_config = super(LLLocalClusterCoordinates, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))



class LLObjectCondensation(LossLayerBase):
    '''
    Cluster using truth index and coordinates
    '''

    def __init__(self, *, energy_loss_weight=1., use_energy_weights=False, q_min=0.5, no_beta_norm=False,
                 potential_scaling=1., repulsion_scaling=1., s_b=1., position_loss_weight=1.,
                 classification_loss_weight=1., timing_loss_weight=1., use_spectators=True, beta_loss_scale=1.,
                 use_average_cc_pos=False, payload_rel_threshold=0.1, rel_energy_mse=False, smooth_rep_loss=False,
                 pre_train=False, huber_energy_scale=2., downweight_low_energy=True, n_ccoords=2, energy_den_offset=1.,
                 noise_scaler=1., too_much_beta_scale=0.1, cont_beta_loss=False, log_energy=False, n_classes=0,
                 standard_configuration=None,
                 **kwargs):
        """
        Read carefully before changing parameters

        :param energy_loss_weight:
        :param use_energy_weights:
        :param q_min:
        :param no_beta_norm:
        :param potential_scaling:
        :param repulsion_scaling:
        :param s_b:
        :param position_loss_weight:
        :param classification_loss_weight:
        :param timing_loss_weight:
        :param use_spectators:
        :param beta_loss_scale:
        :param use_average_cc_pos:
        :param payload_rel_threshold:
        :param rel_energy_mse:
        :param smooth_rep_loss:
        :param pre_train:
        :param huber_energy_scale:
        :param downweight_low_energy:
        :param n_ccoords:
        :param energy_den_offset:
        :param noise_scaler:
        :param too_much_beta_scale:
        :param cont_beta_loss:
        :param log_energy:
        :param n_classes: give the real number of classes, in the truth labelling, class 0 is always ignored so if you
                          have 6 classes, label them from 1 to 6 not 0 to 5. If n_classes is 0, no classification loss
                          is applied
        :param standard_configuration:
        :param kwargs:
        """
        if 'dynamic' in kwargs:
            super(LLObjectCondensation, self).__init__(**kwargs)
        else:
            super(LLObjectCondensation, self).__init__(dynamic=True, **kwargs)

        self.energy_loss_weight = energy_loss_weight
        self.use_energy_weights = use_energy_weights
        self.q_min = q_min
        self.no_beta_norm = no_beta_norm
        self.potential_scaling = potential_scaling
        self.repulsion_scaling = repulsion_scaling
        self.s_b = s_b
        self.position_loss_weight = position_loss_weight
        self.classification_loss_weight = classification_loss_weight
        self.timing_loss_weight = timing_loss_weight
        self.use_spectators = use_spectators
        self.beta_loss_scale = beta_loss_scale
        self.use_average_cc_pos = use_average_cc_pos
        self.payload_rel_threshold = payload_rel_threshold
        self.rel_energy_mse = rel_energy_mse
        self.smooth_rep_loss = smooth_rep_loss
        self.pre_train = pre_train
        self.huber_energy_scale = huber_energy_scale
        self.downweight_low_energy = downweight_low_energy
        self.n_ccoords = n_ccoords
        self.energy_den_offset = energy_den_offset
        self.noise_scaler = noise_scaler
        self.too_much_beta_scale = too_much_beta_scale
        self.cont_beta_loss = cont_beta_loss
        self.log_energy = log_energy
        self.n_classes = n_classes

        if standard_configuration is not None:
            raise NotImplemented('Not implemented yet')

    def loss(self, inputs):
        x, truth_dict, pred_dict, feat_dict, row_splits = inputs

        config = {
            'energy_loss_weight': self.energy_loss_weight,
            'use_energy_weights': self.use_energy_weights,
            'q_min': self.q_min,
            'no_beta_norm': self.no_beta_norm,
            'potential_scaling': self.potential_scaling,
            'repulsion_scaling': self.repulsion_scaling,
            's_b': self.s_b,
            'position_loss_weight': self.position_loss_weight,
            'classification_loss_weight' : self.classification_loss_weight,
            'timing_loss_weight': self.timing_loss_weight,
            'use_spectators': self.use_spectators,
            'beta_loss_scale': self.beta_loss_scale,
            'use_average_cc_pos': self.use_average_cc_pos,
            'payload_rel_threshold': self.payload_rel_threshold,
            'rel_energy_mse': self.rel_energy_mse,
            'smooth_rep_loss': self.smooth_rep_loss,
            'pre_train': self.pre_train,
            'huber_energy_scale': self.huber_energy_scale,
            'downweight_low_energy': self.downweight_low_energy,
            'n_ccoords': self.n_ccoords,
            'energy_den_offset': self.energy_den_offset,
            'noise_scaler': self.noise_scaler,
            'too_much_beta_scale': self.too_much_beta_scale,
            'cont_beta_loss': self.cont_beta_loss,
            'log_energy': self.log_energy,
            'n_classes': self.n_classes
        }

        loss = obj_cond_loss(truth_dict, pred_dict, feat_dict, row_splits, config)
        return loss

    def get_config(self):
        config = {
            'energy_loss_weight': self.energy_loss_weight,
            'use_energy_weights': self.use_energy_weights,
            'q_min': self.q_min,
            'no_beta_norm': self.no_beta_norm,
            'potential_scaling': self.potential_scaling,
            'repulsion_scaling': self.repulsion_scaling,
            's_b': self.s_b,
            'position_loss_weight': self.position_loss_weight,
            'classification_loss_weight' : self.classification_loss_weight,
            'timing_loss_weight': self.timing_loss_weight,
            'use_spectators': self.use_spectators,
            'beta_loss_scale': self.beta_loss_scale,
            'use_average_cc_pos': self.use_average_cc_pos,
            'payload_rel_threshold': self.payload_rel_threshold,
            'rel_energy_mse': self.rel_energy_mse,
            'smooth_rep_loss': self.smooth_rep_loss,
            'pre_train': self.pre_train,
            'huber_energy_scale': self.huber_energy_scale,
            'downweight_low_energy': self.downweight_low_energy,
            'n_ccoords': self.n_ccoords,
            'energy_den_offset': self.energy_den_offset,
            'noise_scaler': self.noise_scaler,
            'too_much_beta_scale': self.too_much_beta_scale,
            'cont_beta_loss': self.cont_beta_loss,
            'log_energy': self.log_energy,
            'n_classes': self.n_classes
        }
        base_config = super(LLObjectCondensation, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))



### LLSelectedDistances
# slim
# takes neighbour indices and distances, and (truth) ass idx
# make indices TF compat by 
#   overwrite = tile( tf range)
#   tf.where(idx<0, overwrite, idx)
# gather asso idx
# select asso idx (no rs necessary already taken care of by kNN)
# repulsive/attractive using distances (will be 0 for self index)
#


class LLFullObjectCondensation(LossLayerBase):
    '''
    Cluster using truth index and coordinates
    
    This is a copy of the above, reducing the nested function calls.
    
    keep the individual loss definitions as separate functions, even if they are trivial.
    inherit from this class to implement different variants of the loss ingredients without
    making the config explode (more)
    '''

    def __init__(self, *, energy_loss_weight=1., use_energy_weights=True, q_min=0.5, no_beta_norm=False,
                 potential_scaling=1., repulsion_scaling=1., s_b=1., position_loss_weight=1.,
                 classification_loss_weight=1., timing_loss_weight=1., use_spectators=True, beta_loss_scale=1.,
                 use_average_cc_pos=0.,
                  payload_rel_threshold=0.1, rel_energy_mse=False, smooth_rep_loss=False,
                 pre_train=False, huber_energy_scale=-1., downweight_low_energy=True, n_ccoords=2, energy_den_offset=1.,
                 noise_scaler=1., too_much_beta_scale=0., cont_beta_loss=False, log_energy=False, n_classes=0,
                 prob_repulsion=False,
                 phase_transition=0.,
                 phase_transition_double_weight=False,
                 alt_potential_norm=False,
                 print_time=True,
                 cut_payload_beta_gradient=False,
                 payload_beta_clip=0.1,
                 kalpha_damping_strength=0.,
                 standard_configuration=None,
                 **kwargs):
        """
        Read carefully before changing parameters

        :param energy_loss_weight:
        :param use_energy_weights:
        :param q_min:
        :param no_beta_norm:
        :param potential_scaling:
        :param repulsion_scaling:
        :param s_b:
        :param position_loss_weight:
        :param classification_loss_weight:
        :param timing_loss_weight:
        :param use_spectators:
        :param beta_loss_scale:
        :param use_average_cc_pos: weight (between 0 and 1) of the average position vs. the kalpha position 
        :param payload_rel_threshold:
        :param rel_energy_mse:
        :param smooth_rep_loss:
        :param pre_train:
        :param huber_energy_scale:
        :param downweight_low_energy:
        :param n_ccoords:
        :param energy_den_offset:
        :param noise_scaler:
        :param too_much_beta_scale:
        :param cont_beta_loss:
        :param log_energy:
        :param n_classes: give the real number of classes, in the truth labelling, class 0 is always ignored so if you
                          have 6 classes, label them from 1 to 6 not 0 to 5. If n_classes is 0, no classification loss
                          is applied
        :param prob_repulsion
        :param phase_transition
        :param standard_configuration:
        :param kwargs:
        """
        if 'dynamic' in kwargs:
            super(LLFullObjectCondensation, self).__init__(**kwargs)
        else:
            super(LLFullObjectCondensation, self).__init__(dynamic=True,**kwargs)

        self.energy_loss_weight = energy_loss_weight
        self.use_energy_weights = use_energy_weights
        self.q_min = q_min
        self.no_beta_norm = no_beta_norm
        self.potential_scaling = potential_scaling
        self.repulsion_scaling = repulsion_scaling
        self.s_b = s_b
        self.position_loss_weight = position_loss_weight
        self.classification_loss_weight = classification_loss_weight
        self.timing_loss_weight = timing_loss_weight
        self.use_spectators = use_spectators
        self.beta_loss_scale = beta_loss_scale
        self.use_average_cc_pos = use_average_cc_pos
        self.payload_rel_threshold = payload_rel_threshold
        self.rel_energy_mse = rel_energy_mse
        self.smooth_rep_loss = smooth_rep_loss
        self.pre_train = pre_train
        self.huber_energy_scale = huber_energy_scale
        self.downweight_low_energy = downweight_low_energy
        self.n_ccoords = n_ccoords
        self.energy_den_offset = energy_den_offset
        self.noise_scaler = noise_scaler
        self.too_much_beta_scale = too_much_beta_scale
        self.cont_beta_loss = cont_beta_loss
        self.log_energy = log_energy
        self.n_classes = n_classes
        self.prob_repulsion = prob_repulsion
        self.phase_transition = phase_transition
        self.phase_transition_double_weight = phase_transition_double_weight
        self.alt_potential_norm = alt_potential_norm
        self.print_time = print_time
        self.cut_payload_beta_gradient = cut_payload_beta_gradient
        self.payload_beta_clip = payload_beta_clip
        self.kalpha_damping_strength = kalpha_damping_strength
        self.loc_time=time.time()
        
        assert kalpha_damping_strength >= 0. and kalpha_damping_strength <= 1.

        if standard_configuration is not None:
            raise NotImplemented('Not implemented yet')
        
        
    def calc_energy_weights(self, t_energy):
        lower_cut = 0.5
        w = tf.where(t_energy > 10., 1., ((t_energy-lower_cut) / 10.)*10./(10.-lower_cut))
        return tf.nn.relu(w)
            
    def calc_energy_loss(self, t_energy, pred_energy): 
        if not self.energy_loss_weight:
            return pred_energy**2 #just learn 0
        
        #FIXME: this is just for debugging
        #return (t_energy-pred_energy)**2
        
        if self.huber_energy_scale > 0:
            l = tf.abs(t_energy-pred_energy)
            sqrt_t_e = tf.sqrt(t_energy+1e-3)
            l = tf.math.divide_no_nan(l, tf.sqrt(t_energy+1e-3) + self.energy_den_offset)
            return huber(l, sqrt_t_e*self.huber_energy_scale)
        else:
            return tf.math.divide_no_nan((t_energy-pred_energy)**2,(t_energy + self.energy_den_offset))

    
    def calc_position_loss(self, t_pos, pred_pos):
        if not self.position_loss_weight:
            t_pos = 0.
        
        return tf.reduce_sum((t_pos-pred_pos) ** 2, axis=-1, keepdims=True)/(10**2) #is in cm
    
    def calc_timing_loss(self, t_time, pred_time):
        if not self.timing_loss_weight:
            return pred_time**2
        
        return (t_time - pred_time)**2 #rechit time is in ns, true time in s
    
    def calc_classification_loss(self, t_pid, pred_id):
        '''
        to be implemented, t_pid is not one-hot encoded
        '''
        return 1e-8*tf.reduce_mean(pred_id**2,axis=-1,keepdims=True) #V x 1
    
    def loss(self, inputs):
        
        start_time = 0
        if self.print_time:
            start_time = time.time()
        
        
        pred_beta, pred_ccoords, pred_energy, pred_pos, pred_time, pred_id,\
        t_idx, t_energy, t_pos, t_time, t_pid,\
        rowsplits = inputs
        
        
        pred_beta    = tf.debugging.check_numerics(pred_beta, "pred_beta has NaNs")
        pred_ccoords    = tf.debugging.check_numerics(pred_ccoords, "pred_ccoords has NaNs")
        pred_energy    = tf.debugging.check_numerics(pred_energy, "pred_energy has NaNs")
        pred_pos    = tf.debugging.check_numerics(pred_pos, "pred_pos has NaNs")
        pred_time    = tf.debugging.check_numerics(pred_time, "pred_time has NaNs")
        pred_id    = tf.debugging.check_numerics(pred_id, "pred_id has NaNs")
        
        if rowsplits.shape[0] is None:
            return tf.constant(0,dtype='float32')
        
        energy_weights = self.calc_energy_weights(t_energy)
        if not self.use_energy_weights:
            energy_weights = tf.zeros_like(energy_weights)+1.
            
        
        t_energy    = tf.debugging.check_numerics(t_energy, "t_energy has NaNs")
        #t_energy = tf.where(t_energy<=0,0.,t_energy)
        pred_energy    = tf.debugging.check_numerics(pred_energy, "pred_energy has NaNs")
        energy_weights    = tf.debugging.check_numerics(energy_weights, "energy_weights has NaNs")
            
        #also kill any gradients for zero weight
        energy_loss = self.energy_loss_weight * self.calc_energy_loss(t_energy, pred_energy)
        position_loss = self.position_loss_weight * self.calc_position_loss(t_pos, pred_pos)
        timing_loss = self.timing_loss_weight * self.calc_timing_loss(t_time, pred_time)
        classification_loss = self.classification_loss_weight * self.calc_classification_loss(t_pid, pred_id)
        
        full_payload = tf.concat([energy_loss,position_loss,timing_loss,classification_loss], axis=-1)
        
        if self.payload_beta_clip > 0:
            full_payload = tf.where(pred_beta<self.payload_beta_clip, 0., full_payload)
            #clip not weight, so there is no gradient to push below threshold!
        
        is_spectator = tf.zeros_like(pred_beta) #not used right now, and likely never again (if the truth remains ok)
        
        att, rep, noise, min_b, payload, exceed_beta = oc_loss(
                                           x=pred_ccoords,
                                           beta=pred_beta,
                                           truth_indices=t_idx,
                                           row_splits=rowsplits,
                                           is_spectator=is_spectator,
                                           payload_loss=full_payload,
                                           Q_MIN=self.q_min,
                                           S_B=self.s_b,
                                           energyweights=energy_weights,
                                           use_average_cc_pos=self.use_average_cc_pos,
                                           payload_rel_threshold=self.payload_rel_threshold,
                                           cont_beta_loss=self.cont_beta_loss,
                                           prob_repulsion=self.prob_repulsion,
                                           phase_transition=self.phase_transition>0. ,
                                           phase_transition_double_weight = self.phase_transition_double_weight,
                                           alt_potential_norm=self.alt_potential_norm,
                                           cut_payload_beta_gradient=self.cut_payload_beta_gradient,
                                           kalpha_damping_strength = self.kalpha_damping_strength
                                           )

        
        
        energy_loss = tf.debugging.check_numerics(att, "att loss has NaNs")
        energy_loss = tf.debugging.check_numerics(rep, "rep loss has NaNs")
        energy_loss = tf.debugging.check_numerics(min_b, "min_b loss has NaNs")
        energy_loss = tf.debugging.check_numerics(noise, "noise loss has NaNs")
        
        att *= self.potential_scaling
        rep *= self.potential_scaling * self.repulsion_scaling
        min_b *= self.beta_loss_scale
        noise *= self.noise_scaler
        exceed_beta *= self.too_much_beta_scale

        
        energy_loss = tf.debugging.check_numerics(payload[0], "energy loss has NaNs")
        pos_loss    = tf.debugging.check_numerics(payload[1], "position loss has NaNs")
        time_loss   = tf.debugging.check_numerics(payload[2], "time loss has NaNs")
        class_loss  = tf.debugging.check_numerics(payload[3], "classification loss has NaNs")
        
        lossval = att + rep + min_b + noise + energy_loss + pos_loss + time_loss + class_loss + exceed_beta
            
        lossval = tf.debugging.check_numerics(lossval, "loss has nan")
        lossval = tf.reduce_mean(lossval)
        
        if self.print_time:
            print('loss layer',self.name,'took',int((time.time()-start_time)*100000.)/100.,'ms')
            print('loss layer info:',self.name,'batch took',int((time.time()-self.loc_time)*100000.)/100.,'ms')
            self.loc_time = time.time()
            
        if self.print_loss:
            minbtext = 'min_beta_loss'
            if self.phase_transition>0:
                minbtext = 'phase transition loss'
                print('avg beta', tf.reduce_mean(pred_beta))
            print('loss', lossval.numpy(),
                  'attractive_loss', att.numpy(),
                  'rep_loss', rep.numpy(),
                  minbtext, min_b.numpy(),
                  'noise_loss', noise.numpy(),
                  'energy_loss', energy_loss.numpy(),
                  'pos_loss', pos_loss.numpy(),
                  'time_loss', time_loss.numpy(),
                  'class_loss', class_loss.numpy(),
                  'exceed_beta', exceed_beta.numpy(),'\n')

        return lossval

    def get_config(self):
        config = {
            'energy_loss_weight': self.energy_loss_weight,
            'use_energy_weights': self.use_energy_weights,
            'q_min': self.q_min,
            'no_beta_norm': self.no_beta_norm,
            'potential_scaling': self.potential_scaling,
            'repulsion_scaling': self.repulsion_scaling,
            's_b': self.s_b,
            'position_loss_weight': self.position_loss_weight,
            'classification_loss_weight' : self.classification_loss_weight,
            'timing_loss_weight': self.timing_loss_weight,
            'use_spectators': self.use_spectators,
            'beta_loss_scale': self.beta_loss_scale,
            'use_average_cc_pos': self.use_average_cc_pos,
            'payload_rel_threshold': self.payload_rel_threshold,
            'rel_energy_mse': self.rel_energy_mse,
            'smooth_rep_loss': self.smooth_rep_loss,
            'pre_train': self.pre_train,
            'huber_energy_scale': self.huber_energy_scale,
            'downweight_low_energy': self.downweight_low_energy,
            'n_ccoords': self.n_ccoords,
            'energy_den_offset': self.energy_den_offset,
            'noise_scaler': self.noise_scaler,
            'too_much_beta_scale': self.too_much_beta_scale,
            'cont_beta_loss': self.cont_beta_loss,
            'log_energy': self.log_energy,
            'n_classes': self.n_classes,
            'prob_repulsion': self.prob_repulsion,
            'phase_transition': self.phase_transition,
            'phase_transition_double_weight': self.phase_transition_double_weight,
            'alt_potential_norm': self.alt_potential_norm,
            'print_time' : self.print_time,
            'cut_payload_beta_gradient': self.cut_payload_beta_gradient,
            'payload_beta_clip' : self.payload_beta_clip,
            'kalpha_damping_strength' : self.kalpha_damping_strength
        }
        base_config = super(LLFullObjectCondensation, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))




class LLFullTrackMLObjectCondensation(LossLayerBase):
    '''
    Cluster using truth index and coordinates
    
    This is a copy of the above, reducing the nested function calls.
    
    keep the individual loss definitions as separate functions, even if they are trivial.
    inherit from this class to implement different variants of the loss ingredients without
    making the config explode (more)
    '''

    def __init__(self, *, energy_loss_weight=1., use_energy_weights=True, q_min=0.5, no_beta_norm=False,
                 potential_scaling=1., repulsion_scaling=1., s_b=1., position_loss_weight=1.,
                 classification_loss_weight=1., timing_loss_weight=1., use_spectators=True, beta_loss_scale=1.,
                 use_average_cc_pos=False, payload_rel_threshold=0.1, rel_energy_mse=False, smooth_rep_loss=False,
                 pre_train=False, huber_energy_scale=2., downweight_low_energy=True, n_ccoords=2, energy_den_offset=1.,
                 noise_scaler=1., too_much_beta_scale=0., cont_beta_loss=False, log_energy=False, n_classes=0,
                 prob_repulsion=False,
                 phase_transition=0.,
                 alt_potential_norm=False,
                 print_time=True,
                 standard_configuration=None,
                 **kwargs):
        """
        Read carefully before changing parameters

        :param energy_loss_weight:
        :param use_energy_weights:
        :param q_min:
        :param no_beta_norm:
        :param potential_scaling:
        :param repulsion_scaling:
        :param s_b:
        :param position_loss_weight:
        :param classification_loss_weight:
        :param timing_loss_weight:
        :param use_spectators:
        :param beta_loss_scale:
        :param use_average_cc_pos:
        :param payload_rel_threshold:
        :param rel_energy_mse:
        :param smooth_rep_loss:
        :param pre_train:
        :param huber_energy_scale:
        :param downweight_low_energy:
        :param n_ccoords:
        :param energy_den_offset:
        :param noise_scaler:
        :param too_much_beta_scale:
        :param cont_beta_loss:
        :param log_energy:
        :param n_classes: give the real number of classes, in the truth labelling, class 0 is always ignored so if you
                          have 6 classes, label them from 1 to 6 not 0 to 5. If n_classes is 0, no classification loss
                          is applied
        :param prob_repulsion
        :param phase_transition
        :param standard_configuration:
        :param kwargs:
        """
        if 'dynamic' in kwargs:
            super(LLFullTrackMLObjectCondensation, self).__init__(**kwargs)
        else:
            super(LLFullTrackMLObjectCondensation, self).__init__(dynamic=True,**kwargs)

        self.energy_loss_weight = energy_loss_weight
        self.use_energy_weights = use_energy_weights
        self.q_min = q_min
        self.no_beta_norm = no_beta_norm
        self.potential_scaling = potential_scaling
        self.repulsion_scaling = repulsion_scaling
        self.s_b = s_b
        self.position_loss_weight = position_loss_weight
        self.classification_loss_weight = classification_loss_weight
        self.timing_loss_weight = timing_loss_weight
        self.use_spectators = use_spectators
        self.beta_loss_scale = beta_loss_scale
        self.use_average_cc_pos = use_average_cc_pos
        self.payload_rel_threshold = payload_rel_threshold
        self.rel_energy_mse = rel_energy_mse
        self.smooth_rep_loss = smooth_rep_loss
        self.pre_train = pre_train
        self.huber_energy_scale = huber_energy_scale
        self.downweight_low_energy = downweight_low_energy
        self.n_ccoords = n_ccoords
        self.energy_den_offset = energy_den_offset
        self.noise_scaler = noise_scaler
        self.too_much_beta_scale = too_much_beta_scale
        self.cont_beta_loss = cont_beta_loss
        self.log_energy = log_energy
        self.n_classes = n_classes
        self.prob_repulsion = prob_repulsion
        self.phase_transition = phase_transition
        self.alt_potential_norm = alt_potential_norm
        self.print_time = print_time

        self.loc_time=time.time()

        if standard_configuration is not None:
            raise NotImplemented('Not implemented yet')
        
        
    def calc_energy_weights(self, t_energy, flatat=10.):
        return tf.where(t_energy > 10., 1., (t_energy / flatat + 0.1)/1.1)
            
    def calc_energy_loss(self, t_energy, pred_energy): 
        return (t_energy-pred_energy)**2/(t_energy**2)

    
    def calc_position_loss(self, t_pos, pred_pos):
        outl = (t_pos-pred_pos)**2/(t_pos**2)
        return tf.reduce_sum(outl, axis=-1, keepdims=True)
    
    def calc_timing_loss(self, t_time, pred_time):
        return (t_time*1e9 - pred_time)**2 #rechit time is in ns, true time in s
    
    def calc_classification_loss(self, t_pid, pred_id):
        '''
        to be implemented, t_pid is not one-hot encoded
        '''
        return 1e-8*tf.reduce_mean(pred_id**2,axis=-1,keepdims=True) #V x 1
    
    def loss(self, inputs):
        
        start_time = 0
        if self.print_time:
            start_time = time.time()
        
        
        pred_beta, pred_ccoords, pred_energy, pred_pos, pred_time, pred_id,\
        t_idx, t_energy, t_pos, t_time, t_pid,\
        rowsplits = inputs
        
        if rowsplits.shape[0] is None:
            return tf.constant(0,dtype='float32')
        
        #these are the weights
        energy_weights = t_time 
            
        #pz
        energy_loss = self.energy_loss_weight * self.calc_energy_loss(t_energy, pred_energy)
        
        #px py
        position_loss = self.position_loss_weight * self.calc_position_loss(t_pos, pred_pos)
        
        #0
        timing_loss = self.timing_loss_weight * self.calc_timing_loss(t_time, pred_time)
        
        #0
        classification_loss = self.classification_loss_weight * self.calc_classification_loss(t_pid, pred_id)
        
        full_payload = tf.concat([energy_loss,position_loss], axis=-1)
        
        is_spectator = tf.zeros_like(pred_beta) #not used right now, and likely never again (if the truth remains ok)
        
        att, rep, noise, min_b, payload, exceed_beta = oc_loss(
                                           x=pred_ccoords,
                                           beta=pred_beta,
                                           truth_indices=t_idx,
                                           row_splits=rowsplits,
                                           is_spectator=is_spectator,
                                           payload_loss=full_payload,
                                           Q_MIN=self.q_min,
                                           S_B=self.s_b,
                                           energyweights=energy_weights,
                                           use_average_cc_pos=self.use_average_cc_pos,
                                           payload_rel_threshold=self.payload_rel_threshold,
                                           cont_beta_loss=self.cont_beta_loss,
                                           prob_repulsion=self.prob_repulsion,
                                           phase_transition=self.phase_transition>0. ,
                                           alt_potential_norm=self.alt_potential_norm
                                           )

        
        att *= self.potential_scaling
        rep *= self.potential_scaling * self.repulsion_scaling
        min_b *= self.beta_loss_scale
        noise *= self.noise_scaler
        exceed_beta *= self.too_much_beta_scale

        
        energy_loss = tf.debugging.check_numerics(payload[0], "pz loss has NaN")
        pos_loss = tf.debugging.check_numerics(payload[1], "px py loss has NaN")
        
        lossval = att + rep + min_b + noise + energy_loss + pos_loss + exceed_beta
            
        lossval = tf.debugging.check_numerics(lossval, "loss has nan")
        lossval = tf.reduce_mean(lossval)
        
        if self.print_time:
            print('loss layer',self.name,'took',int((time.time()-start_time)*100000.)/100.,'ms')
            print('loss layer info:',self.name,'batch took',int((time.time()-self.loc_time)*100000.)/100.,'ms')
            self.loc_time = time.time()
            
        if self.print_loss:
            minbtext = 'min_beta_loss'
            if self.phase_transition>0:
                minbtext = 'phase transition loss'
                print('avg beta', tf.reduce_mean(pred_beta))
            print('loss', lossval.numpy(),
                  'attractive_loss', att.numpy(),
                  'rep_loss', rep.numpy(),
                  minbtext, min_b.numpy(),
                  'noise_loss', noise.numpy(),
                  'pz', energy_loss.numpy(),
                  'px py', pos_loss.numpy(),
                  'exceed_beta', exceed_beta.numpy(),'\n')

        return lossval

    def get_config(self):
        config = {
            'energy_loss_weight': self.energy_loss_weight,
            'use_energy_weights': self.use_energy_weights,
            'q_min': self.q_min,
            'no_beta_norm': self.no_beta_norm,
            'potential_scaling': self.potential_scaling,
            'repulsion_scaling': self.repulsion_scaling,
            's_b': self.s_b,
            'position_loss_weight': self.position_loss_weight,
            'classification_loss_weight' : self.classification_loss_weight,
            'timing_loss_weight': self.timing_loss_weight,
            'use_spectators': self.use_spectators,
            'beta_loss_scale': self.beta_loss_scale,
            'use_average_cc_pos': self.use_average_cc_pos,
            'payload_rel_threshold': self.payload_rel_threshold,
            'rel_energy_mse': self.rel_energy_mse,
            'smooth_rep_loss': self.smooth_rep_loss,
            'pre_train': self.pre_train,
            'huber_energy_scale': self.huber_energy_scale,
            'downweight_low_energy': self.downweight_low_energy,
            'n_ccoords': self.n_ccoords,
            'energy_den_offset': self.energy_den_offset,
            'noise_scaler': self.noise_scaler,
            'too_much_beta_scale': self.too_much_beta_scale,
            'cont_beta_loss': self.cont_beta_loss,
            'log_energy': self.log_energy,
            'n_classes': self.n_classes,
            'prob_repulsion': self.prob_repulsion,
            'phase_transition': self.phase_transition,
            'alt_potential_norm': self.alt_potential_norm,
            'print_time' : self.print_time
        }
        base_config = super(LLFullTrackMLObjectCondensation, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))



        