import numpy as np
import shapely
import openslide
import cv2

from shapely.geometry import Polygon, MultiPolygon, GeometryCollection
from shapely.strtree import STRtree
from shapely.ops import unary_union

from paquo.projects import QuPathProject
from mothi.utils import label_img_to_polys, _round_polygon


class QuPathTilingProject(QuPathProject):
    def __init__(self, path, mode = 'r'):
        ''' load or create a new qupath project

        Parameters
        ----------
        path:
            path to `project.qpproj` file, or its parent directory
        mode:
            'r' --> readonly, error if not there
            'r+' --> read/write, error if not there
            'a' = 'a+' --> read/write, create if not there, append if there
            'w' = 'w+' --> read/write, create if not there, truncate if there
            'x' = 'x+' --> read/write, create if not there, error if there
        '''
        super().__init__(path, mode)
        self._class_dict = {}
        for i, ann in enumerate(self.path_classes):
            self._class_dict[i] = ann
        self._inverse_class_dict = {value.id: key for key, value in self._class_dict.items()}
        self.img_annot_dict = {}


    def update_path_classes(self, path_classes):
        ''' update the annotation classes and annotation dictionaries of the project
        
        Parameters
        ----------
        path_classes: 
            annotation classes to set
        '''
        self.path_classes = path_classes
        self._class_dict = {}
        for i, ann in enumerate(self.path_classes):
            self._class_dict[i] = ann
        self._inverse_class_dict = {value.id: key for key, value in self._class_dict.items()}

    
    def update_img_annot_dict(self, img_id):
        ''' update annotation rois tree for faster shapely queries

        Parameters
        ----------
        img_id:
            id of image to operate
        '''
        slide = self.images[img_id]
        annotations = slide.hierarchy.annotations
        img_ann_list = [(annot.roi, annot.path_class.id) for annot in annotations]

        img_ann_transposed = np.array(img_ann_list, dtype = object).transpose() # [list(rois), list(annot_classes)]
        class_by_id = dict((id(ann_poly), (i, img_ann_transposed[1][i])) for i, ann_poly in enumerate(img_ann_transposed[0]))
        img_ann_tree = STRtree(img_ann_transposed[0])
        self.img_annot_dict[img_id] = (img_ann_tree, class_by_id)


    def get_tile(self, img_id, location, size, downsample_level = 0):
        ''' get tile starting at x|y (slide level 0) with given size  

        Parameters
        ----------
        img_id:
            id of image to operate
        location:
            (x, y) tuple containing coordinates for the top left pixel in the level 0 slide
        size:
            (width, height) tuple containing the tile size
        downsample_level:
            level for downsampling

        Returns
        -------
        tile: _
            tile image
        '''
        slide = self.images[img_id]
        with openslide.open_slide(slide.uri.removeprefix('file://')) as slide_data:
            tile = slide_data.read_region(location, downsample_level, size)
        return(tile)


    def get_tile_annot(self, img_id, location, size, class_filter = None):
        ''' get tile annotations between (x|y) and (x + size| y + size)

        Parameters
        ----------
        img_id:
            id of image to operate
        location:
            (x, y) tuple containing coordinates for the top left pixel in the level 0 slide
        size:
            (width, height) tuple containing the tile size
        class_filter:
            list of annotationclass names or ids to filter by
            if None no filter is applied

        Returns
        -------
        tile_intersections: _
            list of annotations (shapely polygons) in tile
        '''
        slide = self.images[img_id]
        hier_data = slide.hierarchy.annotations
        location_x, location_y = location
        width, height = size
        polygon_tile = Polygon(([location_x, location_y], [location_x + width, location_y], [location_x + width, location_y + height], [location_x, location_y + height]))
        tile_intersections = []

        if img_id in self.img_annot_dict:
            ann_tree, index_and_class = self.img_annot_dict[img_id]
            near_polys = [poly for poly in ann_tree.query(polygon_tile)]
            near_poly_classes = [index_and_class[id(poly)][1] for poly in near_polys]
            for poly, annot_class in zip(near_polys, near_poly_classes):
                intersection = poly.intersection(polygon_tile)
                if intersection.is_empty:
                    continue
                
                filter_bool = (not class_filter) or (annot_class in class_filter) or (self._inverse_class_dict[annot_class] in class_filter)

                if filter_bool and (isinstance(intersection, MultiPolygon) or isinstance(intersection, GeometryCollection)): # filter applies and polygon is a multipolygon
                    for inter in intersection.geoms:
                        if isinstance(inter, Polygon):
                            tile_intersections.append((annot_class, inter))
                
                elif filter_bool: # filter applies and is not a multipolygon
                    tile_intersections.append((annot_class, intersection))

        else:
            img_ann_list = []
            for annot in hier_data:
                if not annot.path_class:
                    continue
                annot_class = annot.path_class.id
                polygon_annot = annot.roi
                img_ann_list.append((polygon_annot, annot_class)) # save all Polygons in list to create a cache.

                intersection = polygon_annot.intersection(polygon_tile)
                if intersection.is_empty:
                    continue

                filter_bool = (not class_filter) or (annot_class in class_filter) or (self._inverse_class_dict[annot_class] in class_filter)  

                if filter_bool and (isinstance(intersection, MultiPolygon) or isinstance(intersection, GeometryCollection)): # filter applies and polygon is a multipolygon
                    for inter in intersection.geoms:
                        if isinstance(inter, Polygon):
                            tile_intersections.append((annot_class, inter))

                elif filter_bool: # filter applies and is not a multipolygon
                    tile_intersections.append((annot_class, intersection))

            img_ann_transposed = np.array(img_ann_list, dtype = object).transpose() # [list(rois), list(annotation_classes)]
            class_by_id = dict((id(ann_poly), (i, img_ann_transposed[1][i])) for i, ann_poly in enumerate(img_ann_transposed[0]))
            img_ann_tree = STRtree(img_ann_transposed[0])
            self.img_annot_dict[img_id] = (img_ann_tree, class_by_id)

        return tile_intersections


    def get_tile_annot_mask(self, img_id, location, size, downsample_level = 0, multilabel = False, class_filter = None):
        ''' get tile annotations mask between (x|y) and (x + size| y + size)

        Parameters
        ----------
        img_id:
            id of image to operate
        location:
            (x, y) tuple containing coordinates for the top left pixel in the level 0 slide
        size:
            (width, height) tuple containing the tile size
        downsample_level:
            level for downsampling
        multilabel:
            if True annotation mask contains boolean image for each class ([num_classes, width, height])
        class_filter:
            list of annotationclass names to filter by

        Returns
        -------
        annot_mask: _
            mask [height, width] with an annotation class for each pixel
            or [num_class, height, width] for multilabels
            background class is ignored for multilabels ([0, height, width] shows mask for the first annotation class)
        '''
        location_x, location_y = location
        width, height = size
        downsample_factor = 2 ** downsample_level 
        level_0_size = map(lambda x: x* downsample_factor, size) # level_0_size needed to get all Polygons in downsampled area
        tile_intersections = self.get_tile_annot(img_id, location, level_0_size, class_filter)

        if multilabel:
            num_classes = len(self.path_classes) -1 
            annot_mask = np.zeros((num_classes, height, width), dtype = np.uint8)

        else:
            # sort intersections descending by area. Now we can not accidentally overwrite polys with other poly holes
            sorted_intersections = sorted(tile_intersections, key = lambda tup: Polygon(tup[1].exterior).area, reverse=True)
            tile_intersections = sorted_intersections
            annot_mask = np.zeros((height, width), dtype = np.uint8)
        

        for inter_class, intersection in tile_intersections:
            class_num = self._inverse_class_dict[inter_class]
            if multilabel: # first class should be on the lowest level for multilabels
                class_num -= 1

            trans_inter = shapely.affinity.translate(intersection, location_x * -1, location_y * -1)
            # apply downsampling by scaling the Polygon down
            scale_inter = shapely.affinity.scale(trans_inter, xfact = 1/downsample_factor, yfact = 1/downsample_factor, origin = (0,0)) 

            exteriors, interiors = _round_polygon(scale_inter)

            if multilabel:
                cv2.fillPoly(annot_mask[class_num], [exteriors], 1)
                cv2.fillPoly(annot_mask[class_num], interiors, 0)

            else:
                cv2.fillPoly(annot_mask, [exteriors], class_num)
                cv2.fillPoly(annot_mask, interiors, 0)

        return annot_mask


    def save_mask_annotations(self, img_id, annot_mask, location = (0,0), downsample_level = 0, min_polygon_area = 0, multilabel = False):
        ''' saves a mask as annotations to QuPath

        Parameters
        ----------
        img_id:
            id of image to operate
        annot_mask:
            mask with annotations
        location:
            (x, y) tuple containing coordinates for the top left pixel in the level 0 slide
        downsample_level:
            level for downsampling
        min_polygon_area:
            minimal area for polygons to be saved
        multilabel:
            if True annotation mask contains boolean image for each class ([num_classes, width, height])
        '''
        slide = self.images[img_id]
        poly_annot_list = label_img_to_polys(annot_mask, downsample_level, min_polygon_area, multilabel)
        for annot_poly, annot_class in poly_annot_list:
            poly_to_add = shapely.affinity.translate(annot_poly, location[0], location[1])
            slide.hierarchy.add_annotation(poly_to_add, self._class_dict[annot_class])


    def merge_near_annotations(self, img_id, max_dist):
        ''' merge nearby annotations with equivalent annotation class

        Parameters
        ----------
        img_id:
            id of image to operate
        max_dist:
            maximal distance between annotations to merge
        '''
        hierarchy = self.images[img_id].hierarchy
        annotations = hierarchy.annotations
        self.update_img_annot_dict(img_id)
        already_merged = [] # save merged indicies
        ann_tree, class_by_id = self.img_annot_dict[img_id]

        for index, annot in enumerate(annotations):
            if index in already_merged:
                annotations.discard(annot)
                continue
            annot_poly = annot.roi
            annot_poly_class = annot.path_class.id
            annot_poly_buffered = annot_poly.buffer(max_dist)

            annotations_to_merge = [annot_poly_buffered]

            nested_annotations = [annot_poly_buffered]
            while len(nested_annotations) > 0:
                annot_poly_buffered = nested_annotations.pop(0)
                near_polys = [poly for poly in ann_tree.query(annot_poly_buffered)]
                near_poly_index_and_classes = [class_by_id[id(poly)] for poly in near_polys]

                while len(near_polys) > 0:
                    near_poly = near_polys.pop(0)
                    near_poly_index, near_poly_annotation_class = near_poly_index_and_classes.pop(0)

                    if near_poly_index in already_merged:
                        continue
                    if index == near_poly_index: # tree query will always return the polygon from the same annotation
                        continue
                    if annot_poly_class != near_poly_annotation_class:
                        continue

                    near_poly_buffered = near_poly.buffer(max_dist)
                    intersects = near_poly_buffered.intersects(annot_poly_buffered)
                    if intersects:     
                        annotations_to_merge.append(near_poly_buffered)
                        nested_annotations.append(near_poly_buffered)
                        already_merged.append(near_poly_index)

            if len(annotations_to_merge) > 1:
                merged_annot = unary_union(annotations_to_merge).buffer(-max_dist)
                hierarchy.add_annotation(merged_annot, self._class_dict[self._inverse_class_dict[annot_poly_class]])
                annotations.discard(annot)