#!/usr/bin/env python3
#-*- coding: utf-8 -*-
"""
@author: huangjunjie
@file: sigat.py
@time: 2018/12/10
"""

import sys
import os
import time
import random
import subprocess
from collections import defaultdict
import argparse

import numpy as np
import scipy.sparse as sp
import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F

from common import DATASET_NUM_DIC
#
from fea_extra import FeaExtra


# Training settings
parser = argparse.ArgumentParser()
parser.add_argument('--devices', type=str, default='cpu', help='Devices')
parser.add_argument('--seed', type=int, default=15, help='Random seed.')
parser.add_argument('--epochs', type=int, default=100, help='Number of epochs to train.')
parser.add_argument('--lr', type=float, default=0.0005, help='Initial learning rate.')
parser.add_argument('--weight_decay', type=float, default=0.0001, help='Weight decay (L2 loss on parameters).')
parser.add_argument('--dataset', default='bitcoin_alpha', help='Dataset')
parser.add_argument('--dim', type=int, default=20, help='Embedding Dimension')
parser.add_argument('--fea_dim', type=int, default=20, help='Feature Embedding Dimension')
parser.add_argument('--batch_size', type=int, default=500, help='Batch Size')
parser.add_argument('--dropout', type=float, default=0.0, help='Dropout k')
parser.add_argument('--k', default=1, help='Folder k')
parser.add_argument('--method', type=str, help="mulsigat or sigat")

args = parser.parse_args()
OUTPUT_DIR = './embeddings/'+args.method
print(OUTPUT_DIR)
if not os.path.exists('embeddings'):
    os.mkdir('embeddings')
if not os.path.exists(OUTPUT_DIR):
    os.mkdir(OUTPUT_DIR)



random.seed(args.seed)
np.random.seed(args.seed)
torch.manual_seed(args.seed)

NEG_LOSS_RATIO = 1
INTERVAL_PRINT = 2

NUM_NODE = DATASET_NUM_DIC[args.dataset]
WEIGHT_DECAY = args.weight_decay
NODE_FEAT_SIZE = args.fea_dim
EMBEDDING_SIZE1 = args.dim
DEVICES = torch.device(args.devices)
LEARNING_RATE = args.lr
BATCH_SIZE = args.batch_size
EPOCHS = args.epochs
DROUPOUT = args.dropout
K = args.k
print(DEVICES)


class Encoder(nn.Module):
    """
    Encode features to embeddings
    """

    def __init__(self, features_lists, feature_dim, embed_dim, adj_lists, aggs):
        super(Encoder, self).__init__()

        self.features_lists = features_lists
        self.feat_dim = feature_dim
        self.adj_lists = adj_lists
        self.aggs = aggs

        self.embed_dim = embed_dim
        for i, agg in enumerate(self.aggs):
            self.add_module('agg_{}'.format(i), agg)
            self.aggs[i] = agg.to(DEVICES)

        def init_weights(m):
            if type(m) == nn.Linear:
                torch.nn.init.kaiming_normal_(m.weight)
                m.bias.data.fill_(0.01)

        self.nonlinear_layer = nn.Sequential(
            nn.Linear(self.feat_dim * (len(adj_lists) + 1), self.feat_dim),
            nn.Tanh(),
            nn.Linear(self.feat_dim, self.embed_dim),

        )
        self.nonlinear_layer.apply(init_weights)


    def forward(self, nodes):
        """
        Generates embeddings for nodes.
        """
        neigh_feats = [agg.forward(nodes, adj) for adj, agg in zip(self.adj_lists, self.aggs)]
        self_feats = self.features_lists[0](torch.LongTensor(nodes).to(DEVICES))
        combined = torch.cat([self_feats] + neigh_feats, 1)
        combined = self.nonlinear_layer(combined)
        return combined

class SpecialSpmmFunction(torch.autograd.Function):
    """Special function for only sparse region backpropataion layer."""
    @staticmethod
    def forward(ctx, indices, values, shape, b):
        assert indices.requires_grad == False
        a = torch.sparse_coo_tensor(indices, values, shape, device=DEVICES)
        ctx.save_for_backward(a, b)
        ctx.N = shape[0]
        return torch.matmul(a, b)

    @staticmethod
    def backward(ctx, grad_output):
        a, b = ctx.saved_tensors
        grad_values = grad_b = None
        if ctx.needs_input_grad[1]:
            grad_a_dense = grad_output.matmul(b.t())
            edge_idx = a._indices()[0, :] * ctx.N + a._indices()[1, :]
            grad_values = grad_a_dense.view(-1)[edge_idx]
        if ctx.needs_input_grad[3]:
            grad_b = a.t().matmul(grad_output)
        return None, grad_values, None, grad_b


class SpecialSpmm(nn.Module):
    def forward(self, indices, values, shape, b):
        return SpecialSpmmFunction.apply(indices, values, shape, b)




class AttentionAggregator(nn.Module):
    def __init__(self, features, in_dim, out_dim, node_num,  dropout_rate=DROUPOUT, slope_ratio=0.1):
        super(AttentionAggregator, self).__init__()

        self.features = features
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.dropout = nn.Dropout(dropout_rate)
        self.slope_ratio = slope_ratio
        self.a = nn.Parameter(torch.FloatTensor(out_dim * 2, 1))
        nn.init.kaiming_normal_(self.a.data)
        self.speical_spmm = SpecialSpmm()

        self.out_linear_layer = nn.Linear(self.in_dim, self.out_dim)
        self.unique_nodes_dict = np.zeros(node_num, dtype=np.int32)


    def forward(self, nodes, adj):
        """
        nodes --- list of nodes in a batch
        adj --- sp.csr_matrix
        """
        node_pku = np.array(nodes)
        edges = np.array(adj[nodes, :].nonzero()).T
        edges[:, 0] = node_pku[edges[:, 0]]

        unique_nodes_list = np.unique(np.hstack((np.unique(edges), np.array(nodes))))

        batch_node_num = len(unique_nodes_list)
        # this dict can map new i to originial node id
        self.unique_nodes_dict[unique_nodes_list] = np.arange(batch_node_num)

        edges[:, 0] = self.unique_nodes_dict[edges[:, 0]]
        edges[:, 1] = self.unique_nodes_dict[edges[:, 1]]

        n2 = torch.LongTensor(unique_nodes_list).to(DEVICES)
        new_embeddings = self.out_linear_layer(self.features(n2))

        original_node_edge = np.array([self.unique_nodes_dict[nodes], self.unique_nodes_dict[nodes]]).T
        edges = np.vstack((edges, original_node_edge))

        edges = torch.LongTensor(edges).to(DEVICES)

        edge_h_2 = torch.cat((new_embeddings[edges[:, 0], :], new_embeddings[edges[:, 1], :]), dim=1)

        edges_h = torch.exp(F.leaky_relu(torch.einsum("ij,jl->il", [edge_h_2, self.a]), self.slope_ratio))
        indices = edges

        row_sum = self.speical_spmm(edges.t(), edges_h[:, 0], torch.Size((batch_node_num, batch_node_num)), torch.ones(size=(batch_node_num, 1)).to(DEVICES))


        results = self.speical_spmm(edges.t(), edges_h[:, 0], torch.Size((batch_node_num, batch_node_num)), new_embeddings)

        output_emb = results.div(row_sum)

        return output_emb[self.unique_nodes_dict[nodes]]


class SiGAT(nn.Module):

    def __init__(self, enc):
        super(SiGAT, self).__init__()
        self.enc = enc

    def forward(self, nodes):
        embeds = self.enc(nodes)
        return embeds

    def criterion(self, nodes, pos_neighbors, neg_neighbors):
        pos_neighbors_list = [set.union(pos_neighbors[i]) for i in nodes]
        neg_neighbors_list = [set.union(neg_neighbors[i]) for i in nodes]
        unique_nodes_list = list(set.union(*pos_neighbors_list).union(*neg_neighbors_list).union(nodes))
        unique_nodes_dict = {n: i for i, n in enumerate(unique_nodes_list)}
        nodes_embs = self.enc(unique_nodes_list)

        loss_total = 0
        for index, node in enumerate(nodes):
            z1 = nodes_embs[unique_nodes_dict[node], :]
            pos_neigs = list([unique_nodes_dict[i] for i in pos_neighbors[node]])
            neg_neigs = list([unique_nodes_dict[i] for i in neg_neighbors[node]])
            pos_num = len(pos_neigs)
            neg_num = len(neg_neigs)

            if pos_num > 0:
                pos_neig_embs = nodes_embs[pos_neigs, :]
                loss_pku = -1 * torch.sum(F.logsigmoid(torch.einsum("nj,j->n", [pos_neig_embs, z1])))
                loss_total += loss_pku
            tmp_pku = 1 if neg_num == 0 else neg_num
            C = pos_num // tmp_pku
            if C == 0:
                C = 1
            if neg_num > 0:
                neg_neig_embs = nodes_embs[neg_neigs, :]
                loss_pku = -1 * torch.sum(F.logsigmoid(-1 * torch.einsum("nj,j->n",[neg_neig_embs, z1])))
                loss_total += C * NEG_LOSS_RATIO  * loss_pku

        return loss_total


def load_data2(filename='', add_public_foe=True):

    adj_lists1   = defaultdict(set) # Undirected case: {u,v} if share a positive edge
    adj_lists1_1 = defaultdict(set) # Directed case: (u,v) if a positive edge from u to v
    adj_lists1_2 = defaultdict(set) # Directed case: (v,u) if a positive edge from u to v
    adj_lists2   = defaultdict(set) # Undirected case: {u,v} if share a negative edge
    adj_lists2_1 = defaultdict(set) # Directed case: (u,v) if a negative edge from u to v
    adj_lists2_2 = defaultdict(set) # Directed case: (v,u) if a negative edge from u to v
    adj_lists3   = defaultdict(set) # Undirected case: {u,v} if any interaction present


    with open(filename) as fp:
        for i, line in enumerate(fp):
            info = line.strip().split()
            person1 = int(info[0])
            person2 = int(info[1])
            v = int(info[2])
            adj_lists3[person2].add(person1)
            adj_lists3[person1].add(person2)

            if v == 1:
                adj_lists1[person1].add(person2)
                adj_lists1[person2].add(person1)

                adj_lists1_1[person1].add(person2)
                adj_lists1_2[person2].add(person1)
            else:
                adj_lists2[person1].add(person2)
                adj_lists2[person2].add(person1)

                adj_lists2_1[person1].add(person2)
                adj_lists2_2[person2].add(person1)


    return adj_lists1, adj_lists1_1, adj_lists1_2, adj_lists2, adj_lists2_1, adj_lists2_2, adj_lists3


def read_emb(num_nodes, fpath):
    dim = 0
    embeddings = 0
    with open(fpath) as f:
        for i, line in enumerate(f.readlines()):
            if i == 0:
                dim = int(line.split()[1])
                embeddings = np.random.rand(num_nodes, dim)
            else:
                line_l = line.split()
                node = line_l[0]
                emb = [float(j) for j in line_l[1:]]
                assert len(emb) == dim
                embeddings[int(node)] = np.array(emb)
    return embeddings

def run( dataset='bitcoin_alpha', k=2):
    num_nodes = DATASET_NUM_DIC[dataset] + 3

    # adj_lists1, adj_lists2, adj_lists3 = load_data(k, dataset)
    filename = './experiment-data/{}-train-{}.edgelist'.format(dataset, k)
    adj_lists1, adj_lists1_1, adj_lists1_2, adj_lists2, adj_lists2_1, adj_lists2_2, adj_lists3 = load_data2(filename, add_public_foe=False)
    print(k, dataset, 'data load!')
    features = nn.Embedding(num_nodes, NODE_FEAT_SIZE)
    features.weight.requires_grad = True

    features.to(DEVICES)

    adj_lists = [adj_lists1, adj_lists1_1, adj_lists1_2, adj_lists2, adj_lists2_1, adj_lists2_2]


    #######
    fea_model = FeaExtra(dataset=dataset, k=k)
    adj_additions1 = [defaultdict(set) for _ in range(16)]
    adj_additions2 = [defaultdict(set) for _ in range(16)]
    adj_additions0 = [defaultdict(set) for _ in range(16)]
    a, b = 0, 0

    for i in adj_lists3:
        for j in adj_lists3[i]:
            v_list = fea_model.feature_part2(i, j)
            for index, v in enumerate(v_list):
                if v > 0:
                    adj_additions0[index][i].add(j)

    for i in adj_lists1_1:
        for j in adj_lists1_1[i]:
            v_list = fea_model.feature_part2(i, j)
            for index, v in enumerate(v_list):
                if v > 0:
                    adj_additions1[index][i].add(j)
                    a += 1

    for i in adj_lists2_1:
        for j in adj_lists2_1[i]:
            v_list = fea_model.feature_part2(i, j)
            for index, v in enumerate(v_list):
                if v > 0:
                    adj_additions2[index][i].add(j)
                    b += 1
    assert a > 0, 'positive something wrong'
    assert b > 0, 'negative something wrong'

    # 38
    # adj_lists = adj_lists + adj_additions1 + adj_additions2
    
    #adj_lists = adj_lists + adj_additions1 + adj_additions2 + [adj_lists3]
    ########################

    # 2
    # adj_lists = [adj_lists1, adj_lists2] # Only considering the undirected graph

    # 4
    # adj_lists = [ adj_lists1_1, adj_lists1_2, adj_lists2_1, adj_lists2_2]

    # 6
    # adj_lists = [adj_lists1, adj_lists1_1, adj_lists1_2, adj_lists2, adj_lists2_1, adj_lists2_2]

    # 10
    # dir_adj_lists = [adj_lists1_1, adj_lists1_2, adj_lists2_1, adj_lists2_2]
    # adj_lists = [adj_lists1, adj_lists2, *dir_adj_lists, *dir_adj_lists]

    # 18
    # adj_lists = adj_lists + adj_additions0

    if args.method == 'mulsigat':
        # 4
        adj_lists = [ adj_lists1_1, adj_lists1_2, adj_lists2_1, adj_lists2_2]
    else:
        # 38
        adj_lists = adj_lists + adj_additions1 + adj_additions2  


    print(len(adj_lists), 'motifs')

    def func(adj_list):
        edges = []
        for a in adj_list:
            for b in adj_list[a]:
                edges.append((a, b))
        edges = np.array(edges)
        adj = sp.csr_matrix((np.ones(len(edges)), (edges[:,0], edges[:,1])), shape=(num_nodes, num_nodes))
        return adj
    if args.method == 'mulsigat':
        adj_lists = [*adj_lists, *adj_lists, *adj_lists]
    adj_lists = list(map(func, adj_lists))
    features_lists = [features for _ in range(len(adj_lists))]
    aggs = [AttentionAggregator(features, NODE_FEAT_SIZE, NODE_FEAT_SIZE, num_nodes) for features, adj in
            zip(features_lists, adj_lists)]
    enc1 = Encoder(features_lists, NODE_FEAT_SIZE, EMBEDDING_SIZE1, adj_lists, aggs)

    model = SiGAT(enc1)
    model.to(DEVICES)
    print(model.train())
    optimizer = torch.optim.Adam(filter(lambda p: p.requires_grad,
                                        list(model.parameters()) + list(enc1.parameters()) \
                                        + list(features.parameters())),
                                 lr=LEARNING_RATE,
                                 weight_decay=WEIGHT_DECAY
                                 )

    for epoch in range(EPOCHS + 2):
        total_loss = []
        if epoch % INTERVAL_PRINT == 0:
            model.eval()
            all_embedding = np.zeros((NUM_NODE, EMBEDDING_SIZE1))
            for i in range(0, NUM_NODE, BATCH_SIZE):
                begin_index = i
                end_index = i + BATCH_SIZE if i + BATCH_SIZE < NUM_NODE else NUM_NODE
                values = np.arange(begin_index, end_index)
                embed = model.forward(values.tolist())
                embed = embed.data.cpu().numpy()
                all_embedding[begin_index: end_index] = embed

            fpath = os.path.join(OUTPUT_DIR, 'embedding-{}-{}-{}.npy'.format(dataset, k, str(epoch)) )
            np.save(fpath, all_embedding)
            model.train()

        time1 = time.time()
        nodes_pku = np.random.permutation(NUM_NODE).tolist()
        for batch in range(NUM_NODE // BATCH_SIZE):
            optimizer.zero_grad()
            b_index = batch * BATCH_SIZE
            e_index = (batch + 1) * BATCH_SIZE
            nodes = nodes_pku[b_index:e_index]

            loss = model.criterion(
                nodes, adj_lists1, adj_lists2
            )
            total_loss.append(loss.data.cpu().numpy())

            loss.backward()
            optimizer.step()
        print(f'epoch: {epoch}, loss: {np.sum(total_loss)}, time: {time.time()-time1}')


def main():
    print('NUM_NODE', NUM_NODE)
    print('WEIGHT_DECAY', WEIGHT_DECAY)
    print('NODE_FEAT_SIZE', NODE_FEAT_SIZE)
    print('EMBEDDING_SIZE1', EMBEDDING_SIZE1)
    print('LEARNING_RATE', LEARNING_RATE)
    print('BATCH_SIZE', BATCH_SIZE)
    print('EPOCHS', EPOCHS)
    print('DROUPOUT', DROUPOUT)
    run(dataset=args.dataset, k=K)


if __name__ == "__main__":
    main()
