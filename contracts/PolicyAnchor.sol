// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title PolicyAnchor — Agent 支付策略的链上承诺锚
/// @notice 只存哈希承诺，不存策略全文（隐私与完整性兼得）。
///         每个地址维护自己的单调递增版本序列，仅本人可写，任何人可读。
contract PolicyAnchor {
    /// owner => version => 策略文档哈希（keccak256 of canonical JSON）
    mapping(address => mapping(uint256 => bytes32)) public anchors;
    /// owner => 已锚定的最新版本号
    mapping(address => uint256) public latestVersion;

    event Anchored(address indexed owner, uint256 indexed version, bytes32 docHash);

    /// @notice 锚定新版本承诺。版本号必须严格递增（与链下版本链一致）。
    function anchor(bytes32 docHash, uint256 version) external {
        require(version == latestVersion[msg.sender] + 1, "version must increment by 1");
        require(docHash != bytes32(0), "empty hash");
        latestVersion[msg.sender] = version;
        anchors[msg.sender][version] = docHash;
        emit Anchored(msg.sender, version, docHash);
    }

    /// @notice 任何人可核对某版本承诺是否与链下文档一致
    function isAnchored(address owner, uint256 version, bytes32 docHash)
        external view returns (bool)
    {
        return anchors[owner][version] == docHash;
    }
}
