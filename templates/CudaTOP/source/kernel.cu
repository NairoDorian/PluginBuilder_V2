/* Shared Use License: This file is owned by Derivative Inc. (Derivative)
* and can only be used, and/or modified for use, in conjunction with
* Derivative's TouchDesigner software, and only if you are a licensee who has
* accepted Derivative's TouchDesigner license or assignment agreement
* (which also govern the use of this file). You may share or redistribute
* a modified version of this file provided the following conditions are met:
*
* 1. The shared file or redistribution must retain the information set out
* above and this list of conditions.
* 2. Derivative's name (Derivative Inc.) or its trademarks may not be used
* to endorse or promote products derived from this file without specific
* prior written permission from Derivative.
*/

// CUDA kernels for CudaTOP. doCUDAOperation() is called by CudaTOP::execute()
// between beginCUDAOperations() and endCUDAOperations().

#include "CPlusPlus_Common.h"
#include "cuda_runtime.h"
#include "device_launch_parameters.h"

#include <algorithm>
#include <stdio.h>

// copyTextureRGBA82D - device kernel: byte-wise copy of an 8-bit BGRA 2D
// surface from input to output, one thread per pixel.
__global__ void
copyTextureRGBA82D(int width, int height, cudaSurfaceObject_t input, cudaSurfaceObject_t output)
{
	unsigned int x = blockIdx.x * blockDim.x + threadIdx.x;
	unsigned int y = blockIdx.y * blockDim.y + threadIdx.y;

	if (x >= width || y >= height)
		return;

	uchar4 color;
	surf2Dread(&color, input, x * 4, y, cudaBoundaryModeZero);
	surf2Dwrite(color, output, x * 4, y, cudaBoundaryModeZero);
}

// copyTextureRGBA83D - device kernel: 3D volume copy of an 8-bit BGRA surface.
__global__ void
copyTextureRGBA83D(int width, int height, cudaSurfaceObject_t input, cudaSurfaceObject_t output)
{
	unsigned int x = blockIdx.x * blockDim.x + threadIdx.x;
	unsigned int y = blockIdx.y * blockDim.y + threadIdx.y;
	unsigned int z = blockIdx.z * blockDim.z + threadIdx.z;

	if (x >= width || y >= height)
		return;

	uchar4 color;
	surf3Dread(&color, input, x * 4, y, z, cudaBoundaryModeZero);
	surf3Dwrite(color, output, x * 4, y, z, cudaBoundaryModeZero);
}

// copyTextureRGBA8Cube - device kernel: cubemap face copy (6 faces via z).
__global__ void
copyTextureRGBA8Cube(int width, int height, cudaSurfaceObject_t input, cudaSurfaceObject_t output)
{
	unsigned int x = blockIdx.x * blockDim.x + threadIdx.x;
	unsigned int y = blockIdx.y * blockDim.y + threadIdx.y;
	unsigned int z = blockIdx.z * blockDim.z + threadIdx.z;

	if (x >= width || y >= height)
		return;

	uchar4 color;
	surfCubemapread(&color, input, x * 4, y, z, cudaBoundaryModeZero);
	surfCubemapwrite(color, output, x * 4, y, z, cudaBoundaryModeZero);
}

// copyTextureRGBA82DArray - device kernel: layered 2D array copy.
__global__ void
copyTextureRGBA82DArray(int width, int height, cudaSurfaceObject_t input, cudaSurfaceObject_t output)
{
	unsigned int x = blockIdx.x * blockDim.x + threadIdx.x;
	unsigned int y = blockIdx.y * blockDim.y + threadIdx.y;
	unsigned int z = blockIdx.z * blockDim.z + threadIdx.z;

	if (x >= width || y >= height)
		return;

	uchar4 color;
	surf2DLayeredread(&color, input, x * 4, y, z, cudaBoundaryModeZero);
	surf2DLayeredwrite(color, output, x * 4, y, z, cudaBoundaryModeZero);
}

// fillOutput - device kernel: solid-color fill used when no input is connected.
__global__ void
fillOutput(int width, int height, uchar4 color, cudaSurfaceObject_t output)
{
	unsigned int x = blockIdx.x * blockDim.x + threadIdx.x;
	unsigned int y = blockIdx.y * blockDim.y + threadIdx.y;

	if (x >= width || y >= height)
		return;

	surf2Dwrite(color, output, x * 4, y, cudaBoundaryModeZero);
}

int
divUp(int a, int b)
{
	return ((a % b) != 0) ? (a / b + 1) : (a / b);
}

// Host entry point. Copies the input with the kernel matching its OP_TexDim, or
// fills the output with 'color' when there is no input, on 'stream'. Returns the
// launch status (and, in Debug builds, the status after the kernel has run).
cudaError_t
doCUDAOperation(int width, int height, int depth, TD::OP_TexDim dim, float4 color, cudaSurfaceObject_t input, cudaSurfaceObject_t output, cudaStream_t stream)
{
	cudaError_t cudaStatus;

	dim3 blockSize(16, 16, 1);
	dim3 gridSize(divUp(width, blockSize.x), divUp(height, blockSize.y), depth);

	if (input)
	{
		switch (dim)
		{
			case TD::OP_TexDim::e2D:
				copyTextureRGBA82D << <gridSize, blockSize, 0, stream >> > (width, height, input, output);
				break;
			case TD::OP_TexDim::e3D:
				copyTextureRGBA83D << <gridSize, blockSize, 0, stream >> > (width, height, input, output);
				break;
			case TD::OP_TexDim::eCube:
				gridSize.z = 6;
				copyTextureRGBA8Cube << <gridSize, blockSize, 0, stream >> > (width, height, input, output);
				break;
			case TD::OP_TexDim::e2DArray:
				copyTextureRGBA82DArray << <gridSize, blockSize, 0, stream >> > (width, height, input, output);
				break;
		}
	}
	else
	{
		dim3 gridSize(divUp(width, blockSize.x), divUp(height, blockSize.y), 1);
		uchar4 c8;
		// Flip R and B since we are outputting to BGRA8
		c8.z = (uint8_t)std::min(std::max(color.x * 255.0f, 0.0f), 255.0f);
		c8.y = (uint8_t)std::min(std::max(color.y * 255.0f, 0.0f), 255.0f);
		c8.x = (uint8_t)std::min(std::max(color.z * 255.0f, 0.0f), 255.0f);
		c8.w = (uint8_t)std::min(std::max(color.w * 255.0f, 0.0f), 255.0f);
		fillOutput<<<gridSize, blockSize, 0, stream>>> (width, height, c8, output);
	}

	// Launch errors (invalid configuration, bad surface...) are available
	// without synchronizing, so check them in every build and return them to
	// the caller (the official sample only checked in Debug builds).
	cudaStatus = cudaGetLastError();
	if (cudaStatus != cudaSuccess)
	{
		fprintf(stderr, "Kernel launch failed: %s\n", cudaGetErrorString(cudaStatus));
		return cudaStatus;
	}

#ifdef _DEBUG
	// Errors raised while the kernel runs only show up after a sync. Debug
	// only, since it stalls the CPU; waits for this stream only
	// (cudaDeviceSynchronize would also wait for TouchDesigner's own CUDA work).
	cudaStatus = cudaStreamSynchronize(stream);
	if (cudaStatus != cudaSuccess)
	{
		fprintf(stderr, "cudaStreamSynchronize returned error code %d after launching kernel!\n", cudaStatus);
	}
#endif

	return cudaStatus;
}
